import { Check, Copy } from "lucide-react";
import { useEffect, useRef, useState } from "react";

type CopyState = "idle" | "copied" | "failed";

export function CopyMessageButton({ content }: { content: string }) {
  const [state, setState] = useState<CopyState>("idle");
  const resetTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => {
    if (resetTimer.current !== null) clearTimeout(resetTimer.current);
  }, []);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(content);
      setState("copied");
    } catch {
      setState("failed");
    }
    if (resetTimer.current !== null) clearTimeout(resetTimer.current);
    resetTimer.current = setTimeout(() => { setState("idle"); }, 2000);
  };
  const label = state === "copied" ? "Đã sao chép" : state === "failed" ? "Không thể sao chép" : "Sao chép";

  return (
    <button className={`message-action ${state}`} type="button" onClick={() => void copy()} aria-label={label} title={label}>
      {state === "copied" ? <Check size={17} /> : <Copy size={17} />}
      <span className="message-action-label">{label}</span>
    </button>
  );
}
