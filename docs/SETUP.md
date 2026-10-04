# Setup

Phase 1 runs the backend on your machine behind a Cloudflare quick tunnel. Phase 2 runs the same code on AWS Lambda. Everything you configure lives in `.env` (git-ignored); see `.env.example` for every setting.

Defaults: invocation name `talk mode`, model `deepseek/deepseek-v4.1-flash` pinned to DeepInfra, low reasoning, OpenRouter web search (Parallel turbo, $0.001 per search). DeepSeek's own API is excluded by OpenRouter's "no training on paid prompts" privacy setting, which is why the pin is DeepInfra.

**Spend cap:** `BRIDGE_SPEND_CAP_USD`. Every OpenRouter response reports its cost, and the backend keeps a running total in `data/bridge.sqlite3` (DynamoDB on AWS). Once the total reaches the cap it refuses new model calls, and Alexa says the spending limit is used up. Don't delete `data/`: that resets the total.

## Phase 1: local

1. **Install.** Run `make setup`, then set `OPENROUTER_API_KEY` and `BRIDGE_TIMEZONE` in `.env`. Create the key at [openrouter.ai/settings/keys](https://openrouter.ai/settings/keys) and give it a credit limit.

2. **Check the backend without Alexa** (two terminals):
   ```bash
   make replay-serve         # terminal 1: accepts unsigned requests, localhost only
   make replay               # terminal 2: launch, two questions, stop
   ```
   Each line shows what Alexa would say and how long it took. `BAD` means an error or a reply over 8 s.

3. **Create the skill** in the [Alexa Developer Console](https://developer.amazon.com/alexa/console/ask). Sign in with the same Amazon account as your Echo.
   - Create Skill, name it "Talk Mode", pick your English locale, type **Custom**, hosting **Provision your own**, then **Start from scratch**. Turn on "Sync locales" to cover every English locale; the Echo's language must be one of the skill's locales.
   - Build, then Interaction Model, then JSON Editor: paste `skill-package/interactionModels/custom/en-US.json` (it works for any English locale), Save, then Build skill.
   - Copy the skill ID (`amzn1.ask.skill.…`) into `.env` as `BRIDGE_SKILL_ID`.

4. **Expose the backend.** Run `make up`. It refuses to start without `BRIDGE_SKILL_ID` or with signature checks off, then prints an `https://….trycloudflare.com/alexa` URL.
   - In the console, go to Build, then Endpoint, then HTTPS, and paste the URL into Default Region.
   - For the certificate, choose "My development endpoint is a sub-domain of a domain that has a wildcard certificate from a certificate authority", then Save.
   - `make reload` restarts the backend and keeps the URL. `make down` then `make up` gives a new URL, which you must paste again.

5. **Test.** In the console's Test tab, enable "Development" and type "open talk mode", then a question. Then on the Echo: "Alexa, open talk mode" (or "start talk mode"). `data/dev.log` gets a `turn answered in X s` line, with the cost, for every question.

## Phase 2: AWS Lambda

At about 15 questions a day this costs about $0 a month on AWS (Lambda, DynamoDB and SSM free tiers). The model bill stays on OpenRouter.

1. **Credentials.** Configure AWS credentials for an account you own, with `aws configure`, or `aws sso login --profile <name>` plus `AWS_PROFILE=<name>` in `.env`. Check them with `aws sts get-caller-identity`.
2. **Deploy.** Run `make deploy`. The default region is `eu-west-1`; set `AWS_REGION` in `.env` to change it, using a region Alexa supports for your locale.
   - The OpenRouter key goes to SSM Parameter Store as a SecureString.
   - It creates the `talk-mode` CloudFormation stack: the function, a DynamoDB table, 14-day logs, and an Alexa trigger locked to your skill ID.
   - `BRIDGE_ALERT_EMAIL` optionally adds an AWS budget email.
   - The Lambda keeps its own spend total, starting from zero, against its own `BRIDGE_SPEND_CAP_USD`.
3. **Point the skill at Lambda.** In the console, go to Build, then Endpoint, choose **AWS Lambda ARN**, and paste the ARN that deploy printed into Default Region. Stop the tunnel with `make down`.
4. **Verify.** `make invoke` runs a scripted conversation against the deployed function. Then use the Echo.

After a code or `.env` change, run `make deploy` again. To remove everything, run `uv run --extra deploy --env-file .env python deploy/deploy.py destroy --yes`.

MCP servers marked `"lambda": false` (such as the Node-based Simplenote server) don't run on Lambda. The skill works there without them.

## How slow answers are handled

Alexa allows about 8 s per response.
- **At 2.5 s:** if the answer isn't ready, the Echo says "One moment".
- **At 7 s:** the skill says "That one needs a little longer. Say go on to hear it." and keeps working. "Go on" then delivers the answer.
- **On Lambda:** the background answer is asked again by an async copy of the function, so a slow question costs twice its tokens.

Web search is limited to one search per question (`BRIDGE_WEB_SEARCH_MAX_USES=1`). Each extra search adds a model pass of about 1.5 to 2 s.

## Logs and the price of each request

Every OpenRouter response reports what that request cost (`usage.cost`, split into the model's tokens and the search fee). `data/dev.log` gets one line per request and one per question:

```
bridge.llm: openrouter gen-… 5.78s, tokens in 6833 (cached 1152) out 312 (thinking 128), searches 2, cost $0.00193 = model $0.00093 + search $0.00100 | spend now $0.0154 of cap $0.0900
bridge.skill: turn answered in 5.79s (model 5.78s); cost $0.00193 (1 request, 2 searches, …) | Q: '…' | A: '…'
```

- **Slow answers:** they log `turn … deferred at 7.00s`, then `turn … ready in the background after X s; cost …`, then `turn go-on: delivered …`.
- **`make costs`:** the same figures go to `data/requests.jsonl`. This prints today's turns as a table and compares the spend total with OpenRouter's own total for the key.
- **On Lambda:** the lines are in CloudWatch Logs.
