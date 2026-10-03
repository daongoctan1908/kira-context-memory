interface MarkdownNode {
  type: string;
  value?: string;
  children?: MarkdownNode[];
  data?: unknown;
  position?: { end: { offset?: number | undefined } } | undefined;
}

/** Translate KiRa's two inline colors without enabling arbitrary raw HTML. */
export function remarkKiraFormatting() {
  return (tree: MarkdownNode, file: { value: unknown }) => {
    if (typeof file.value === "string") hidePendingSpan(tree, file.value);
    formatChildren(tree);
  };
}

function hidePendingSpan(tree: MarkdownNode, source: string) {
  const fragment = /<\/?(?:s|sp|spa|span(?:\s[^<>]*)?)$/i.exec(source);
  if (fragment === null) return;
  const precedingBackslashes = /\\+$/.exec(source.slice(0, fragment.index))?.[0].length ?? 0;
  if (precedingBackslashes % 2 !== 0) return;

  const trimText = (node: MarkdownNode) => {
    if (node.type === "text" && node.position?.end.offset === source.length && node.value?.endsWith(fragment[0])) {
      node.value = node.value.slice(0, -fragment[0].length);
    }
    node.children?.forEach(trimText);
  };
  trimText(tree);
}

function formatChildren(parent: MarkdownNode) {
  if (parent.children === undefined) return;

  const children: MarkdownNode[] = [];
  const frames = [children];
  for (const child of parent.children) {
    formatChildren(child);
    const current = frames[frames.length - 1] ?? children;
    const html = child.type === "html" ? child.value?.trim() : undefined;

    if (html !== undefined && /^<span\b[^>]*>$/i.test(html) && !/\/\s*>$/.test(html)) {
      const color = spanColor(html);
      if (color !== null) {
        const coloredChildren: MarkdownNode[] = [];
        current.push({
          type: "emphasis",
          children: coloredChildren,
          data: {
            hName: "span",
            hProperties: { className: [`kira-value-${color}`] },
          },
        });
        frames.push(coloredChildren);
      } else {
        // Track unsupported nested spans so their closing tag cannot end a color.
        current.push(child);
        frames.push(current);
      }
    } else if (html !== undefined && /^<\/span\s*>$/i.test(html) && frames.length > 1) {
      frames.pop();
    } else {
      current.push(child);
    }
  }
  parent.children = children;
}

function spanColor(html: string): "positive" | "negative" | null {
  const style = /\sstyle\s*=\s*(["'])(.*?)\1/i.exec(html)?.[2];
  if (style === undefined) return null;
  const color = /(?:^|;)\s*color\s*:\s*(green|red)\s*(?:;|$)/i.exec(style)?.[1]?.toLowerCase();
  return color === "green" ? "positive" : color === "red" ? "negative" : null;
}
