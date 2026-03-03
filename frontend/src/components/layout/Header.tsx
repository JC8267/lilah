import { BarChart3 } from 'lucide-react';

export function Header() {
  return (
    <header className="h-14 border-b border-[var(--color-border)] bg-[var(--color-surface)] flex items-center px-4 gap-3 shrink-0 shadow-sm">
      <div className="w-9 h-9 rounded-lg bg-[var(--color-primary)] flex items-center justify-center">
        <BarChart3 className="w-5 h-5 text-white" />
      </div>
      <div className="flex flex-col">
        <h1 className="text-lg font-semibold leading-tight text-[var(--color-text)]">Lilah</h1>
        <span className="text-[10px] font-semibold tracking-widest uppercase text-[var(--color-text-secondary)]">
          Survey Analytics
        </span>
      </div>
    </header>
  );
}
