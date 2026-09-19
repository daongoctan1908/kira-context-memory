import Markdown from "react-markdown";

export function SafeMarkdown({ content }: { content: string }) {
  return (
    <div className="message-markdown">
      <Markdown skipHtml>{content}</Markdown>
    </div>
  );
}
