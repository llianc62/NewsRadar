# `config/` — project data files

This directory holds **data** configuration files consumed at runtime
(`config.yaml`, `frequency_words.txt`).

⚠️ **Do not add `__init__.py` or any `.py` file here.**

The loader lives in the top-level `config.py` module. Python resolves
`import config` to that module only as long as this directory stays a
plain data folder. If an `__init__.py` appears here, the directory
becomes a package and **shadows** `config.py`, breaking
`from config import load_config` with an `ImportError` at startup.
