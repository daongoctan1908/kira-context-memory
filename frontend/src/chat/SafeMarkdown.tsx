import { Check, Copy } from "lucide-react";
import { Children, isValidElement, useState, type ReactNode } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

export function SafeMarkdown({ content }: { content: string }) {
  return (
    <div className="message-markdown">
      <Markdown
        skipHtml
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ children, href }) => (
            <a href={href} target="_blank" rel="noreferrer noopener">{children}</a>
          ),
          pre: ({ children }) => <CodeBlock code={textContent(children).replace(/\n$/, "")} />,
        }}
      >
        {content}
      </Markdown>
    </div>
  );
}

function CodeBlock({ code }: { code: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      window.setTimeout(() => { setCopied(false); }, 1800);
    } catch {
      setCopied(false);
    }
  };
  return (
    <div className="code-block">
      <div className="code-block-header">
        <span>Mã</span>
        <button type="button" onClick={() => void copy()} aria-label="Sao chép đoạn mã">
          {copied ? <Check size={15} /> : <Copy size={15} />}
          {copied ? "Đã sao chép" : "Sao chép"}
        </button>
      </div>
      <pre><code>{code}</code></pre>
    </div>
  );
}

function textContent(value: ReactNode): string {
  return Children.toArray(value).map((child) => {
    if (typeof child === "string" || typeof child === "number") return String(child);
    return isValidElement<{ children?: ReactNode }>(child) ? textContent(child.props.children) : "";
  }).join("");
}
