import ReactMarkdown from 'react-markdown';
import { User, Bot } from 'lucide-react';
import type { Message } from '../../types';

interface MessageBubbleProps {
  message: Message;
}

const MAX_CHART_SUMMARY_CHARS = 220;

function summarizeChartBackedMessage(content: string): string {
  const lines = content
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean);

  if (lines.length === 0) {
    return 'Analysis completed. See Key Insights for detailed metrics.';
  }

  const headline = lines[0].replace(/^[\-*]\s+/, '').trim();
  if (!headline) {
    return 'Analysis completed. See Key Insights for detailed metrics.';
  }

  if (headline.length <= MAX_CHART_SUMMARY_CHARS) {
    return headline;
  }

  return `${headline.slice(0, MAX_CHART_SUMMARY_CHARS - 1).trimEnd()}…`;
}

export function MessageBubble({ message }: MessageBubbleProps) {
  const isUser = message.role === 'user';
  const hasCharts = !isUser && Array.isArray(message.charts) && message.charts.length > 0;
  const compactSummary = hasCharts
    ? summarizeChartBackedMessage(message.content)
    : '';

  return (
    <div className={`flex gap-3 ${isUser ? 'justify-end' : ''}`}>
      {!isUser && (
        <div className="w-8 h-8 rounded-full bg-[var(--color-primary-light)] flex items-center justify-center shrink-0">
          <Bot className="w-4 h-4 text-[var(--color-primary)]" />
        </div>
      )}
      <div
        className={`max-w-[80%] rounded-2xl px-4 py-3 text-sm leading-relaxed ${
          isUser
            ? 'bg-[var(--color-primary)] text-white'
            : 'bg-[var(--color-surface-alt)] text-[var(--color-text)] border-l-2 border-[var(--color-primary)]/20'
        }`}
      >
        {isUser ? (
          <p className="whitespace-pre-wrap">{message.content}</p>
        ) : hasCharts ? (
          <div>
            <p className="whitespace-pre-wrap">{compactSummary}</p>
            <p className="mt-2 text-xs text-[var(--color-text-secondary)]">
              Full metrics are shown in Key Insights next to the chart.
            </p>
          </div>
        ) : (
          <div className="prose prose-sm max-w-none [&_p]:my-1 [&_ul]:my-1 [&_li]:my-0.5 [&_strong]:font-semibold [&_a]:text-[var(--color-primary)]">
            <ReactMarkdown>{message.content}</ReactMarkdown>
          </div>
        )}
      </div>
      {isUser && (
        <div className="w-8 h-8 rounded-full bg-gray-200 flex items-center justify-center shrink-0">
          <User className="w-4 h-4 text-gray-600" />
        </div>
      )}
    </div>
  );
}
