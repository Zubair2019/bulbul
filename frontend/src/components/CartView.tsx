import { useState } from "react";
import type { CartViewData } from "../lib/events";

const rupees = (v: number | null | undefined) =>
  v == null ? "–" : `₹${Number.isInteger(v) ? v.toLocaleString("en-IN") : v.toFixed(2)}`;

/** The user's store cart as a read-only picture card under Bulbul's reply (open by default). */
export function CartViewCard({ title, cart }: { title: string; cart: CartViewData }) {
  const count = cart.items.reduce((n, i) => n + i.quantity, 0);
  return (
    <details open className="not-prose group mt-3 overflow-hidden rounded-xl border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900">
      <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-4 py-3 text-sm font-medium hover:bg-zinc-50 dark:hover:bg-zinc-800/60">
        <span>
          🛒 {title} <span className="font-normal text-zinc-500">· {count} item{count === 1 ? "" : "s"} · {rupees(cart.to_pay)}</span>
        </span>
        <span className="text-xs text-zinc-500 group-open:hidden">Show</span>
        <span className="hidden text-xs text-zinc-500 group-open:inline">Hide</span>
      </summary>
      <ul className="divide-y divide-zinc-100 border-t border-zinc-200 dark:divide-zinc-800 dark:border-zinc-800">
        {cart.items.map((i, k) => (
          <li key={k} className="flex items-center gap-3 px-4 py-2">
            <Thumb src={i.image_url} name={i.name} />
            <span className="min-w-0 flex-1">
              <span className="block truncate text-sm">{i.name}</span>
              <span className="block text-xs text-zinc-500">
                {i.size}
                {i.price != null && ` · ${i.quantity} × ${rupees(i.price)}`}
              </span>
            </span>
            <span className="text-sm tabular-nums">{rupees(i.line_total ?? (i.price != null ? i.price * i.quantity : null))}</span>
          </li>
        ))}
      </ul>
      <dl className="space-y-0.5 border-t border-zinc-200 px-4 py-3 text-sm dark:border-zinc-800">
        <BillRow label="Items" value={rupees(cart.item_total)} />
        <BillRow label="Delivery" value={cart.delivery === 0 ? "Free" : rupees(cart.delivery)} />
        <BillRow label="Handling" value={rupees(cart.handling)} />
        <BillRow label="To pay" value={rupees(cart.to_pay)} strong />
      </dl>
      {cart.warning && (
        <p className="border-t border-amber-200 bg-amber-50 px-4 py-2 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
          {cart.warning}
        </p>
      )}
    </details>
  );
}

function BillRow({ label, value, strong }: { label: string; value: string; strong?: boolean }) {
  return (
    <div className={`flex justify-between ${strong ? "pt-1 font-semibold" : "text-zinc-600 dark:text-zinc-400"}`}>
      <dt>{label}</dt>
      <dd className="tabular-nums">{value}</dd>
    </div>
  );
}

function Thumb({ src, name }: { src?: string | null; name: string }) {
  const [broken, setBroken] = useState(false);
  return src && !broken ? (
    <img src={src} alt="" onError={() => setBroken(true)} className="h-10 w-10 shrink-0 rounded-md bg-zinc-100 object-cover dark:bg-zinc-800" />
  ) : (
    <span className="grid h-10 w-10 shrink-0 place-items-center rounded-md bg-zinc-100 text-sm font-semibold text-zinc-500 dark:bg-zinc-800">
      {name.charAt(0)}
    </span>
  );
}
