"""Build and deploy the Lambda backend with CloudFormation + boto3.

    uv run --extra deploy --env-file .env python deploy/deploy.py build     # zip only, no AWS calls
    uv run --extra deploy --env-file .env python deploy/deploy.py deploy    # key -> SSM, stack, code
    uv run --extra deploy --env-file .env python deploy/deploy.py destroy --yes

Reads OPENROUTER_API_KEY and BRIDGE_SKILL_ID (plus optional BRIDGE_* model
settings and BRIDGE_ALERT_EMAIL) from the environment. Region: AWS_REGION,
default eu-west-1, which Amazon recommends for skills in the IN/EU locales.
AWS credentials come from the normal AWS profile chain (`aws configure`).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
STACK = "talk-mode"
FUNCTION = "talk-mode"
KEY_PARAM = "/talk-mode/openrouter-api-key"
DEFAULT_REGION = "eu-west-1"
RUNTIME_PACKAGES = ["httpx==0.28.1"]
MCP_PACKAGE = "mcp==2.2.0"
# Modules the Lambda does not need: the web server and HTTPS signature checks.
LOCAL_ONLY = {"server.py", "verify.py"}


def lambda_packages() -> list[str]:
    packages = list(RUNTIME_PACKAGES)
    config = ROOT / "mcp_servers.json"
    if config.exists():
        servers = json.loads(config.read_text()).get("mcpServers") or {}
        # `"lambda": false` servers are local-only and skipped on Lambda (tools.read_specs).
        active = [cfg for cfg in servers.values() if not cfg.get("disabled") and cfg.get("lambda") is not False]
        if active:
            packages.append(MCP_PACKAGE)
            for cfg in active:
                packages.extend((cfg.get("lambda") or {}).get("packages", []))
    return packages


def build() -> Path:
    stage = BUILD / "lambda"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    packages = lambda_packages()
    subprocess.run(
        [
            "uv", "pip", "install", "--quiet", "--target", str(stage),
            "--python-platform", "aarch64-manylinux2014", "--python-version", "3.13",
            "--only-binary", ":all:", *packages,
        ],
        check=True,
    )
    for source in sorted((ROOT / "bridge").rglob("*.py")):
        relative = source.relative_to(ROOT)
        if source.name not in LOCAL_ONLY or source.parent != ROOT / "bridge":
            (stage / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, stage / relative)
    if (ROOT / "mcp_servers.json").exists():
        shutil.copy2(ROOT / "mcp_servers.json", stage / "mcp_servers.json")

    archive = BUILD / "lambda.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(stage.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                zf.write(path, path.relative_to(stage))
    print(f"built {archive} ({archive.stat().st_size / 1e6:.1f} MB) with {', '.join(packages)}")
    return archive


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"{name} is not set (put it in .env)")
    return value


def _session() -> Any:
    import boto3

    return boto3.session.Session(region_name=os.environ.get("AWS_REGION") or DEFAULT_REGION)


def _stack_parameters() -> list[dict[str, str]]:
    env = os.environ
    values = {
        "FunctionName": FUNCTION,
        "SkillId": _require("BRIDGE_SKILL_ID"),
        "ApiKeyParameterName": KEY_PARAM,
        "Model": env.get("BRIDGE_MODEL") or "deepseek/deepseek-v4.1-flash",
        "ProviderOrder": env.get("BRIDGE_PROVIDER_ORDER", "deepinfra"),
        "Reasoning": env.get("BRIDGE_REASONING") or "low",
        "WebSearch": (env.get("BRIDGE_WEB_SEARCH") or "true").lower(),
        "DeadlineSeconds": env.get("BRIDGE_DEADLINE_SECONDS") or "7.0",
        "SpendCapUsd": env.get("BRIDGE_SPEND_CAP_USD") or "0",
        "Timezone": env.get("BRIDGE_TIMEZONE") or "UTC",
        "UserLocation": env.get("BRIDGE_USER_LOCATION", ""),
        "AlertEmail": env.get("BRIDGE_ALERT_EMAIL", ""),
    }
    return [{"ParameterKey": k, "ParameterValue": v} for k, v in values.items()]


def _deploy_stack(cfn: Any) -> dict[str, str]:
    from botocore.exceptions import ClientError

    body = (ROOT / "deploy" / "template.yaml").read_text()
    args = {
        "StackName": STACK,
        "TemplateBody": body,
        "Parameters": _stack_parameters(),
        "Capabilities": ["CAPABILITY_IAM"],
    }
    try:
        cfn.describe_stacks(StackName=STACK)
        exists = True
    except ClientError:
        exists = False
    if exists:
        try:
            cfn.update_stack(**args)
            print("updating stack...")
            cfn.get_waiter("stack_update_complete").wait(StackName=STACK)
        except ClientError as exc:
            if "No updates are to be performed" not in str(exc):
                raise
            print("stack unchanged")
    else:
        cfn.create_stack(**args, OnFailure="DELETE")
        print("creating stack (about 1-2 minutes)...")
        cfn.get_waiter("stack_create_complete").wait(StackName=STACK)
    outputs = cfn.describe_stacks(StackName=STACK)["Stacks"][0].get("Outputs", [])
    return {o["OutputKey"]: o["OutputValue"] for o in outputs}


def deploy() -> None:
    key = _require("OPENROUTER_API_KEY")
    _require("BRIDGE_SKILL_ID")
    archive = build()
    session = _session()
    identity = session.client("sts").get_caller_identity()
    print(f"account {identity['Account']}, region {session.region_name}")

    session.client("ssm").put_parameter(Name=KEY_PARAM, Value=key, Type="SecureString", Overwrite=True)
    print(f"stored the OpenRouter key in SSM {KEY_PARAM} (SecureString)")

    outputs = _deploy_stack(session.client("cloudformation"))

    lam = session.client("lambda")
    lam.update_function_code(FunctionName=FUNCTION, ZipFile=archive.read_bytes())
    lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION)
    print("uploaded code")
    print(
        "\nDone. In the Alexa Developer Console > Build > Endpoint choose AWS Lambda ARN and set\n"
        f"  Default Region: {outputs.get('FunctionArn')}\n"
        "Then: make invoke   (scripted conversation against the deployed function)"
    )


def destroy(confirmed: bool) -> None:
    if not confirmed:
        sys.exit("This deletes the stack (function, table with conversation history, logs) and the stored key. "
                 "Re-run with --yes to confirm.")
    session = _session()
    cfn = session.client("cloudformation")
    cfn.delete_stack(StackName=STACK)
    print("deleting stack...")
    cfn.get_waiter("stack_delete_complete").wait(StackName=STACK)
    ssm = session.client("ssm")
    try:
        ssm.delete_parameter(Name=KEY_PARAM)
    except ssm.exceptions.ParameterNotFound:
        pass
    print("deleted stack and key parameter")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["build", "deploy", "destroy"])
    parser.add_argument("--yes", action="store_true", help="confirm destroy")
    args = parser.parse_args()
    if args.command == "build":
        build()
    elif args.command == "deploy":
        deploy()
    else:
        destroy(args.yes)


if __name__ == "__main__":
    main()
