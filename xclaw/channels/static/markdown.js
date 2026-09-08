/* Keep the Markdown trust boundary here, shared by history and new replies. */
(function (root) {
  "use strict";
  if (typeof root.markdownit !== "function") return;
  const md = root.markdownit({html: false, breaks: true, linkify: false, typographer: false});
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
