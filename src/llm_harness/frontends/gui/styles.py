"""Style constants and page CSS of the web chat UI."""

from llm_harness.frontends.markdown_render import pygments_css

_PRE_WRAP = "whitespace-pre-wrap"
# Secondary text (thinking, usage line, notes) is a little smaller than the 18px base.
_DIM = "text-grey llm-secondary"
_MARKDOWN_CLASS = "llm-md"

# Full-viewport chat layout: the page never scrolls, only the message area does.
# Quasar's own sizes (14px buttons, inputs, expansion header) are raised to match.
_PAGE_CSS = """
html, body { overflow: hidden; }
.nicegui-content { height: 100dvh; padding: 12px; }
.llm-page { font-size: 18px; }
.llm-page .q-field__native, .llm-page .q-field__input,
.llm-page .q-field__label, .llm-page .q-field__marginal { font-size: 20px; }
.llm-page .q-btn { font-size: 18px; }
.llm-page .q-item__label, .llm-secondary { font-size: 16px; }
.llm-page .q-tab__label { font-size: 18px; }
.llm-page .q-scrollarea__content { width: 100%; overflow-wrap: anywhere; }
"""

# The model reply is markdown rendered to sanitized HTML. Wide tables and code
# scroll inside their own box, so the page itself never grows sideways.
_MARKDOWN_CSS = """
.llm-md { line-height: 1.5; }
.llm-md > :first-child { margin-top: 0; }
.llm-md > :last-child { margin-bottom: 0; }
.llm-md h1, .llm-md h2, .llm-md h3, .llm-md h4, .llm-md h5, .llm-md h6 {
  font-weight: 600; line-height: 1.25; margin: 1em 0 0.4em;
}
.llm-md h1 { font-size: 1.5em; }
.llm-md h2 { font-size: 1.35em; }
.llm-md h3 { font-size: 1.2em; }
.llm-md h4, .llm-md h5, .llm-md h6 { font-size: 1.05em; }
.llm-md p, .llm-md ul, .llm-md ol, .llm-md blockquote { margin: 0.5em 0; }
.llm-md ul, .llm-md ol { padding-left: 1.6em; }
.llm-md ul { list-style: disc; }
.llm-md ol { list-style: decimal; }
.llm-md li.task-list-item { list-style: none; margin-left: -1.2em; }
.llm-md input[type=checkbox] { margin-right: 0.4em; }
.llm-md blockquote {
  border-left: 4px solid #bdbdbd; color: #616161; padding: 0 0 0 0.8em;
}
.llm-md table {
  display: block; width: auto; max-width: 100%; overflow-x: auto;
  border-collapse: collapse; margin: 0.6em 0;
}
.llm-md th, .llm-md td { border: 1px solid #bdbdbd; padding: 4px 10px; text-align: left; }
.llm-md th { background: #f5f5f5; font-weight: 600; }
.llm-md pre {
  background: #f5f5f5; border-radius: 4px; margin: 0.6em 0; padding: 8px 12px;
  overflow-x: auto; white-space: pre; overflow-wrap: normal;
}
.llm-md code, .llm-md pre {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 16px;
}
.llm-md :not(pre) > code { background: #f0f0f0; border-radius: 3px; padding: 1px 4px; }
.llm-md a { color: #1565c0; text-decoration: underline; }
.llm-md hr { border: 0; border-top: 1px solid #bdbdbd; margin: 1em 0; }
.llm-md .footnotes { font-size: 16px; }
""" + pygments_css(_MARKDOWN_CLASS)
