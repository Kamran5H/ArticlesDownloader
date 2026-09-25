# Extras

Standalone helper scripts that are **not part of Articles Downloader**. They were kept
from the original repository for reference and are not tested or maintained with the app.

| File | What it is |
| --- | --- |
| `process_cv.py` | Plots a Gamry `.DTA` **cyclic voltammetry** scan with peak analysis (edit the hard-coded file path first). Needs pandas, numpy, matplotlib. |
| `ascii_cv.py` | Prints a cyclic-voltammetry scan from a Gamry `.DTA` file as text. |
| `import_libraries.py` | Checks that numpy, matplotlib, RDKit, PubChemPy, python-pptx and pywin32 import correctly. |
| `search_ddg.py` | One-off DuckDuckGo Lite query example. |
| `FIX_NETWORK_RUN_AS_ADMIN.bat` | Windows network reset. **Warning:** it resets Winsock/TCP-IP and permanently changes the DNS servers of the adapter named "Wi-Fi" to Cloudflare/Google. Read it before running. |
