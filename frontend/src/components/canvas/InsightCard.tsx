import ReactMarkdown from 'react-markdown';
import { Lightbulb } from 'lucide-react';

interface InsightCardProps {
  text: string;
}

export function InsightCard({ text }: InsightCardProps) {
  if (!text.trim()) return null;

  return (
    <div className="border border-[var(--color-border)] rounded-xl bg-[var(--color-surface)] p-5 shadow-sm">
      <div className="flex items-center gap-2 mb-3">
        <div className="w-6 h-6 rounded-full bg-amber-50 flex items-center justify-center">
          <Lightbulb className="w-3.5 h-3.5 text-amber-500" />
        </div>
        <h3 className="text-sm font-semibold text-[var(--color-text)]">Key Insights</h3>
      </div>
      <div className="prose prose-sm max-w-none text-[var(--color-text)] leading-relaxed [&_p]:my-1.5 [&_ul]:my-1.5 [&_li]:my-0.5 [&_strong]:text-[var(--color-text)] [&_strong]:font-semibold">
        <ReactMarkdown>{text}</ReactMarkdown>
      </div>
    </div>
  );
}
