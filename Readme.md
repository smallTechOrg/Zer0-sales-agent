# AI Agent Boilerplate

This project is a simple AI agent powered by Groq cloud (via LangChain), delivered through a Flask backend. 

## Tech Stack

- [Flask](https://flask.palletsprojects.com/en/stable/) – For backend
- [Langchain](https://python.langchain.com/docs/introduction/) – Framework to build Chatbot

## Features

- Fast, production-ready backend (Flask)
- Modular integration with Groq's LLM via LangChain
- Runs on Windows, Linux, and macOS


## Prerequisites

### Install Python 

   - [Windows](https://github.com/Madhyamakist/workspace-setup-windows) 
   - [macOS](https://github.com/Madhyamakist/workspace-setup-mac/blob/dev/python_installation.md)
   <!-- - [Linux](https://github.com/Madhyamakist/workspace-setup-windows/blob/dev/python_installation.md)   -->


### Python version control

#### macOS and Linux

<details>


The Python version needs to be the same as mentioned in the `.tool-versions` file.

Make sure the correct Python version has been set up using `asdf` before you work on this project.
```
python3 --version
```


</details>

#### Windows

<details>


- The Python version needs to be the same as mentioned in the `.python-version` file using pyenv-win.

- Make sure the correct Python version has been set by running `python --version`


</details>

---
### Get a valid Groq API


- Go to [Groq](https://console.groq.com/keys) and create API Key
- Put your Groq API key in `code/.env` file

---
### Setup PostgreSQL


- Install and setup PostgreSQL in you local
- Adjust your username and password in `code/.env` file

## Getting Started

### Clone the repository:

```bash
git clone https://github.com/Madhyamakist/ai-agent-boilerplate.git myproject
```

---

### Set up virtual environment
#### Windows
<details>
- To create a virtual environment called "venv", run

```bash
python -m venv venv
```
-  To activate the environment
```bash
venv\Scripts\activate
```
</details>

#### macOS
<details>

- Create a virtual environment named "venv", 

```bash
python3 -m venv venv
```
-  To activate the environment
```bash
source venv/bin/activate
```
</details>

---

### Install dependencies
After activating your virtual environment, install dependencies by

```bash
cd code
pip install -r requirements.txt
```

## Run Flask to render frontend

### Setup Env

Cope `.env.example` into `.env` (gitignored).
```
cp .env.example .env
```

Replace the values (get it from team members?)
```
GROQ_API_KEY=randomstring
DATABASE_URL=postgresql://username:password@localhost:5432/
```


### Run

If virtual environment is activated and dependencies are installed then run chatbot by:

```bash
cd code
python app.py
```
Now visit http://127.0.0.1:5000/ in your browser.
For newer APIs visit http://127.0.0.1:5001/api/docs.

## chatbot0 public pages

The marketing pages for **chatbot0** (the chat agent this backend powers) are
rendered server-side by the `chatbot0` blueprint (`code/chatbot0/`), under the
`/chatbot0/` prefix, because `/` is the legacy Swagger UI:

| Path | Page |
|---|---|
| `/chatbot0/` | Landing page |
| `/chatbot0/pricing` | Pricing (plans live in `code/chatbot0/content.py`) |
| `/chatbot0/robots.txt`, `/chatbot0/sitemap.xml` | SEO files for chatbot0.smalltech.in |
| `/chatbot0/static/...` | CSS, favicons, logo (`code/static/chatbot0/`) |

Templates are in `code/templates/chatbot0/`. Links in them are relative, so the
same pages work at the root of chatbot0.smalltech.in once that host is mapped
onto `/chatbot0/`. Canonical and Open Graph URLs always use
https://chatbot0.smalltech.in. The pages take no input and never touch the
database or the LLM.

## Test Flask APIs 

If flask is rendered successfully, then test APIs by:

```bash
cd code/test
pytest --html=test_report.html --self-contained-html -v
```


## For Linting

Using ruff (example inside `code/`)
```
ruff check app.py --fix
```

## Deployment

Pushes to `staging` and `main` deploy to Cloud Run through GitHub Actions.
See [DEPLOYMENT.md](DEPLOYMENT.md).
