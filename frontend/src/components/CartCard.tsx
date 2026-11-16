import { useMemo, useState } from "react";
import type { ActionItem, ProposedAction, ResumeValue } from "../lib/events";

const rupees = (v: number) => `₹${Number.isInteger(v) ? v : v.toFixed(2)}`;

/** A proposed cart as picture cards: tick / untick items and change quantities, then approve. */
export function CartCard({
  action,
  agent,
  notice,
  onAnswer,
  disabled,
}: {
  action: ProposedAction;
  agent: string;
  notice?: string; // e.g. a change asked for in words that couldn't be applied
  onAnswer: (v: ResumeValue) => void;
  disabled: boolean;
}) {
  const items = action.items ?? [];
  const [picked, setPicked] = useState<Record<string, boolean>>(() => Object.fromEntries(items.map((i) => [i.spin_id, true])));
  const [qty, setQty] = useState<Record<string, number>>(() => Object.fromEntries(items.map((i) => [i.spin_id, i.quantity])));

  const chosen = items.filter((i) => picked[i.spin_id]);
  const total = useMemo(
    () => chosen.reduce((sum, i) => sum + (i.price ?? 0) * (qty[i.spin_id] ?? 1), 0),
    [chosen, qty],
  );
  const allOn = chosen.length === items.length;
  const toggleAll = () => setPicked(Object.fromEntries(items.map((i) => [i.spin_id, !allOn])));
  const bump = (id: string, d: number) => setQty((q) => ({ ...q, [id]: Math.min(20, Math.max(1, (q[id] ?? 1) + d)) }));

  const approve = () =>
    onAnswer({ approved: true, items: chosen.map((i) => ({ spin_id: i.spin_id, quantity: qty[i.spin_id] ?? 1 })) });

  return (
    <div className="rounded-xl border border-accent-100 bg-accent-50/60 p-4 dark:border-accent-700/40 dark:bg-accent-700/10">
      <div className="mb-3 flex items-baseline justify-between gap-3">
        <div className="text-sm font-semibold text-accent-700 dark:text-accent-100">
          Here's what I found on Instamart <span className="sr-only">({agent})</span>
        </div>
        <button onClick={toggleAll} disabled={disabled} className="shrink-0 text-xs text-zinc-500 hover:text-zinc-900 dark:hover:text-zinc-100">
          {allOn ? "Deselect all" : "Select all"}
        </button>
      </div>

      {notice && (
        <p role="status" className="mb-3 rounded-lg bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
          {notice}
        </p>
      )}

      <ul className="divide-y divide-zinc-200/70 overflow-hidden rounded-lg border border-zinc-200 bg-white dark:divide-zinc-800 dark:border-zinc-800 dark:bg-zinc-900">
        {items.map((i) => (
          <CartRow
            key={i.spin_id}
            item={i}
            on={!!picked[i.spin_id]}
            quantity={qty[i.spin_id] ?? 1}
            disabled={disabled}
            onToggle={() => setPicked((p) => ({ ...p, [i.spin_id]: !p[i.spin_id] }))}
            onBump={(d) => bump(i.spin_id, d)}
          />
        ))}
      </ul>

      <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-zinc-600 dark:text-zinc-400">
          {chosen.length} of {items.length} items · <span className="font-semibold text-zinc-900 dark:text-zinc-100">{rupees(total)}</span>
          <span className="text-xs"> + delivery & handling. Nothing is ordered yet. Tap to change, or just tell me.</span>
        </p>
        <div className="flex gap-2">
          <button onClick={() => onAnswer({ approved: false })} disabled={disabled} className="rounded-lg border border-zinc-300 px-3 py-1.5 text-sm hover:bg-white disabled:opacity-40 dark:border-zinc-700 dark:hover:bg-zinc-800">
            Decline
          </button>
          <button
            onClick={approve}
            disabled={disabled || chosen.length === 0}
            className="rounded-lg bg-accent-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-accent-700 disabled:opacity-40"
          >
            {chosen.length === 0 ? "Select an item" : `Update cart · ${chosen.length} item${chosen.length > 1 ? "s" : ""} · ${rupees(total)}`}
          </button>
        </div>
      </div>
    </div>
  );
}

function CartRow({
  item,
  on,
  quantity,
  disabled,
  onToggle,
  onBump,
}: {
  item: ActionItem;
  on: boolean;
  quantity: number;
  disabled: boolean;
  onToggle: () => void;
  onBump: (d: number) => void;
}) {
  const [broken, setBroken] = useState(false);
  return (
    <li className={`flex items-center gap-3 px-3 py-2.5 transition-opacity ${on ? "" : "opacity-45"}`}>
      <input
        type="checkbox"
        checked={on}
        onChange={onToggle}
        disabled={disabled}
        aria-label={`Include ${item.name}`}
        className="h-4 w-4 shrink-0 accent-[var(--color-accent-600)]"
      />
      <button onClick={onToggle} disabled={disabled} className="flex min-w-0 flex-1 items-center gap-3 text-left" tabIndex={-1}>
        {item.image_url && !broken ? (
          <img
            src={item.image_url}
            alt=""
            loading="eager"
            decoding="async"
            onError={() => setBroken(true)}
            className="h-14 w-14 shrink-0 rounded-lg bg-zinc-100 object-cover dark:bg-zinc-800"
          />
        ) : (
          <span className="grid h-14 w-14 shrink-0 place-items-center rounded-lg bg-zinc-100 text-lg font-semibold text-zinc-500 dark:bg-zinc-800">
            {item.name.charAt(0)}
          </span>
        )}
        <span className="min-w-0">
          <span className={`block truncate text-sm font-medium ${on ? "" : "line-through"}`}>{item.name}</span>
          <span className="block text-xs text-zinc-500">
            {item.size}
            {item.price != null && ` · ${rupees(item.price)} each`}
          </span>
        </span>
      </button>
      <div className="flex shrink-0 items-center rounded-md border border-zinc-300 text-sm dark:border-zinc-700">
        <button onClick={() => onBump(-1)} disabled={disabled || !on || quantity <= 1} className="px-2 py-0.5 disabled:opacity-30" aria-label="Decrease quantity">
          −
        </button>
        <span className="w-6 text-center tabular-nums">{quantity}</span>
        <button onClick={() => onBump(1)} disabled={disabled || !on || quantity >= 20} className="px-2 py-0.5 disabled:opacity-30" aria-label="Increase quantity">
          +
        </button>
      </div>
      <span className={`w-14 shrink-0 text-right text-sm tabular-nums ${on ? "font-medium" : "line-through"}`}>
        {item.price != null ? rupees(item.price * quantity) : "–"}
      </span>
    </li>
  );
}
