import { MessageSquare } from 'lucide-react';

export function Header() {
  return (
    <header className="h-14 border-b border-[var(--color-border)] bg-[var(--color-surface)] flex items-center px-4 gap-3 shrink-0">
      <MessageSquare className="w-6 h-6 text-[var(--color-primary)]" />
      <h1 className="text-lg font-semibold text-[var(--color-text)]">Lilah</h1>
      <span className="text-xs text-[var(--color-text-secondary)] ml-1">Survey Analytics</span>
    </header>
  );
}
