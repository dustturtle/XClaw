# Reproduce the chat screenshot

![Synthetic three-day price comparison in the XClaw web chat](chat.png)

This fixture loads the project's own chat page, stylesheet and Markdown renderer. It supplies one fixed question and answer directly to the message renderer. It exercises presentation only. There is no server, database, live quote, tool execution or model call.

After installing the project in a virtual environment, run from the repository root with a local Chrome or Chromium executable:

~~~sh
python docs/demo/render.py --chrome /path/to/chromium --output docs/demo/chat.png
~~~

Chrome uses a temporary browser profile and renders a 1120 × 760 viewport. Fonts can differ between operating systems. The prices 100.00, 103.00 and 101.97 are synthetic. The daily percentage changes are +3% and −1%.
