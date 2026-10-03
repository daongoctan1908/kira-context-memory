import { Check, Copy, ImageOff } from "lucide-react";
import { Children, isValidElement, useState, type ReactNode } from "react";
import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { remarkKiraFormatting } from "./remarkKiraFormatting";

const markdownComponents: Components = {
  a: ({ children, href }) => (
    <a href={href} target="_blank" rel="noreferrer noopener">{children}</a>
  ),
  pre: ({ children }) => <CodeBlock code={textContent(children).replace(/\n$/, "")} />,
  table: ({ children }) => (
    <div className="message-table-scroll" role="region" aria-label="Bảng số liệu" tabIndex={0}>
      <table>{children}</table>
    </div>
  ),
  th: ({ children, style }) => (
    <th
      data-align={style?.textAlign}
      data-column={/^(stt|xếp hạng|thứ hạng)$/i.test(textContent(children).trim()) ? "rank" : undefined}
    >
      {children}
    </th>
  ),
  td: ({ children, style }) => <td data-align={style?.textAlign}>{children}</td>,
  img: ({ src, alt }) => (
    <MessageImage
      key={typeof src === "string" ? src : "missing"}
      src={typeof src === "string" ? src : undefined}
      alt={alt}
    />
  ),
};

export function SafeMarkdown({ content }: { content: string }) {
  return (
    <div className="message-markdown">
      <Markdown
        skipHtml
        remarkPlugins={[remarkGfm, remarkKiraFormatting]}
        components={markdownComponents}
      >
        {content}
      </Markdown>
    </div>
  );
}

function MessageImage({ src, alt }: { src: string | undefined; alt: string | undefined }) {
  const [failed, setFailed] = useState(false);
  const source = imageSource(src);
  const trimmedAlt = alt?.trim();
  const caption = trimmedAlt === undefined || trimmedAlt.length === 0 ? "Biểu đồ" : trimmedAlt;

  if (source === null || failed) {
    return (
      <span className="message-image message-image-unavailable" role="note">
        <ImageOff size={22} aria-hidden="true" />
        <span>
          <strong className="message-image-title">Biểu đồ chưa khả dụng</strong>
          <span className="message-image-caption">{caption}</span>
        </span>
      </span>
    );
  }

  return (
    <span className="message-image">
      <img
        src={source}
        alt={caption}
        loading="lazy"
        decoding="async"
        referrerPolicy="no-referrer"
        onError={() => { setFailed(true); }}
      />
      <span className="message-image-caption">{caption}</span>
    </span>
  );
}

function imageSource(src: string | undefined): string | null {
  if (src === undefined || src.length === 0) return null;
  if (src.startsWith("/") && !src.startsWith("//") && !src.includes("\\")) return src;

  try {
    const url = new URL(src);
    if (url.username !== "" || url.password !== "") return null;
    // Keep this origin in sync with nginx/security-headers.conf.
    const allowedOrigin = url.origin === "https://charts.vietteltelecom.vn"
      || (typeof window !== "undefined" && url.origin === window.location.origin);
    return allowedOrigin && (url.protocol === "https:" || url.protocol === "http:") ? src : null;
  } catch {
    return null;
  }
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
