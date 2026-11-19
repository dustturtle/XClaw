/* Keep the Markdown trust boundary here, shared by history and new replies. */
(function (root) {
  "use strict";
  if (typeof root.markdownit !== "function") return;
  const md = root.markdownit({html: false, breaks: true, linkify: false, typographer: false});
  // CommonMark treats punctuation before a closing ** followed by CJK text as
  // a non-closing delimiter (e.g. **数据说明：**查询). Support this narrow, plain
  // Chinese-label form at token level; never rewrite code, escapes or raw HTML.
  md.inline.ruler.before("emphasis", "cjk_label_strong", (state, silent) => {
    const start = state.pos;
    if (state.src.slice(start, start + 2) !== "**" || state.src[start + 2] === "*") return false;
    const end = state.src.indexOf("**", start + 2);
    if (end < 0 || end + 2 >= state.posMax) return false;
    const body = state.src.slice(start + 2, end);
    if (!/[\u3400-\u9fff]/.test(body) || !/[，。！？；：、）】》」』]$/.test(body)
        || !/[\p{L}\p{N}]/u.test(state.src[end + 2]) || /[\n\r*`\[\]<>\\]/.test(body)
        || body.trim() !== body) return false;
    if (!silent) {
      state.push("strong_open", "strong", 1);
      state.push("text", "", 0).content = body;
      state.push("strong_close", "strong", -1);
    }
    state.pos = end + 2;
    return true;
  });
  // Never load model-supplied remote images (tracking), including data/SVG URLs.
  md.disable("image");
  const renderLink = md.renderer.rules.link_open;
  md.renderer.rules.link_open = function (tokens, index, options, env, self) {
    tokens[index].attrSet("target", "_blank");
    tokens[index].attrSet("rel", "noopener noreferrer");
    return renderLink ? renderLink(tokens, index, options, env, self) : self.renderToken(tokens, index, options);
  };
  md.renderer.rules.table_open = () => '<div class="table-scroll" tabindex="0" role="region" aria-label="表格"><table>\n';
  md.renderer.rules.table_close = () => "</table></div>\n";
  root.renderChatMarkdown = text => md.render(String(text));
})(typeof window === "undefined" ? globalThis : window);
