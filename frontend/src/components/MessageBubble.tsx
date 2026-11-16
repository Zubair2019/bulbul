import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ChatMessage } from "../hooks/useChat";
import type { Attachment } from "../lib/events";
import { CartViewCard } from "./CartView";

const KIND_LABEL: Partial<Record<ChatMessage["kind"], string>> = {
  confirm_plan: "Proposed plan",
  approve: "Needed your OK",
};

export function MessageBubble({ message, streaming }: { message: ChatMessage; streaming?: boolean }) {
  if (message.role === "user") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-accent-600 px-4 py-2.5 text-[15px] text-white">
          {message.content}
        </div>
      </div>
    );
  }

  const label = KIND_LABEL[message.kind];
  if (label) {
    return (
      <div className="border-l-2 border-accent-500 pl-3 text-sm text-zinc-600 dark:text-zinc-400">
        <div className="mb-0.5 text-xs font-medium uppercase tracking-wide text-accent-600 dark:text-accent-100">{label}</div>
        <div className="whitespace-pre-wrap">{message.content}</div>
      </div>
    );
  }

  return (
    <div className={`prose-chat ${message.kind === "error" ? "text-red-700 dark:text-red-300" : ""}`}>
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
      {streaming && <span className="ml-0.5 inline-block h-4 w-1.5 animate-pulse bg-zinc-400 align-middle" />}
      {message.attachments?.map((a, i) => <AttachmentCard key={i} attachment={a} />)}
    </div>
  );
}

/** A recipe (or other long result) as a card under the spoken reply: collapsed by default. */
function AttachmentCard({ attachment }: { attachment: Attachment }) {
  if (attachment.kind === "cart" && attachment.cart) return <CartViewCard title={attachment.title} cart={attachment.cart} />;
  return (
    <details className="not-prose group mt-3 overflow-hidden rounded-xl border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900">
      <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-4 py-3 text-sm font-medium hover:bg-zinc-50 dark:hover:bg-zinc-800/60">
        <span>📖 {attachment.title}</span>
        <span className="text-xs text-zinc-500 group-open:hidden">Show</span>
        <span className="hidden text-xs text-zinc-500 group-open:inline">Hide</span>
      </summary>
      <div className="prose-chat border-t border-zinc-200 px-4 py-3 text-[14px] dark:border-zinc-800">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{attachment.body}</ReactMarkdown>
      </div>
    </details>
  );
}
