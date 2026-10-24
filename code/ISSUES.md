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