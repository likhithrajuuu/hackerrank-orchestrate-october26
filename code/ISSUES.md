# Issues that I face during the programming 

### 1. Python Version Problem
My computer's venv had the 3.9 version which does not support `str | None` when researched, I figured out that it requires a Python version of the 3.10+ 
So ran the following commands as there were no requirements at this point when I am typing. Please check the following commands


Remove the present Python 3.9
```bash
deactivate 2>/dev/null
rm -rf .venv
```

Create a new virtual environment with Python 3.12
```bash
/opt/homebrew/bin/python3.12 -m venv .venv
source .venv/bin/activate
```

Checking the python version
```bash
python --version
```

### 2. OpenAI rejects `temperature` for the Luna and Sol models
I picked **Luna** as the primary model (reasoning effort `high`) and **Sol** as the escalation model (reasoning effort `low`), and I wanted a low temperature so the classifier would be repeatable. The very first live call failed:

```text
configuration error: primary: OpenAIInvalidRequestError: Error code: 400 - {'error': {'message': "Unsupported parameter: 'temperature' is not supported with this model.", 'type': 'invalid_request_error', 'param': 'temperature', 'code': None}}
```

So with reasoning-effort models OpenAI does not support temperature customization: the model only accepts its own default, and any `temperature` value (even 0) is a 400 error.

**What happened:** the app treated the 400 as a permanent error, stopped with exit code 2 and printed a hint, so nothing was silently flagged.

**Fix:** the code no longer sends a temperature by default (`DEFAULT_TEMPERATURE = "none"` in `code/llm/config.py`). If a model that does accept one is used later, set `LUNA_TEMPERATURE=0` or `SOL_TEMPERATURE=0` in `.env`. Repeatability comes from the schema-validated output and the response cache instead. See D-007 in `DECISIONS.md`.
