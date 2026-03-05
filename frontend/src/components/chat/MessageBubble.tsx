import ReactMarkdown from 'react-markdown';
import { Bot, CheckCircle2, User, XCircle } from 'lucide-react';
import type { Message } from '../../types';

interface MessageBubbleProps {
  message: Message;
}

export function MessageBubble({ message }: MessageBubbleProps) {
  const isUser = message.role === 'user';
  const hasCharts = !isUser && Array.isArray(message.charts) && message.charts.length > 0;
  const filters = message.metadata?.active_filters || [];
  const toolEvents = message.metadata?.tool_events || [];

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
        {filters.length > 0 && (
          <div className={`mb-3 flex flex-wrap gap-2 ${isUser ? 'text-white/90' : ''}`}>
            {filters.map((filter) => (
              <span
                key={`${filter.demo_id}:${filter.demo_level}`}
                className={`inline-flex items-center rounded-full px-2 py-1 text-[11px] font-medium ${
                  isUser
                    ? 'bg-white/15 text-white'
                    : 'bg-white text-[var(--color-primary)]'
                }`}
              >
                {filter.demo_level.replace(/^(TOTAL|CUSTOMER|PROSPECT): /, '')}
              </span>
            ))}
          </div>
        )}
        {isUser ? (
          <p className="whitespace-pre-wrap">{message.content}</p>
        ) : (
          <div className="prose prose-sm max-w-none [&_p]:my-1 [&_ul]:my-1 [&_li]:my-0.5 [&_strong]:font-semibold [&_a]:text-[var(--color-primary)]">
            <ReactMarkdown>{message.content}</ReactMarkdown>
          </div>
        )}
        {!isUser && toolEvents.length > 0 && (
          <details className="mt-3 border-t border-[var(--color-border)]/70 pt-3">
            <summary className="cursor-pointer text-xs font-medium text-[var(--color-text-secondary)]">
              Execution Trace
            </summary>
            <div className="mt-2 space-y-1.5 text-xs text-[var(--color-text-secondary)]">
              {toolEvents.map((event, index) => (
                <div key={`${event.label}-${index}`} className="flex items-start gap-2">
                  {event.status === 'error' ? (
                    <XCircle className="mt-0.5 h-3.5 w-3.5 text-red-500" />
                  ) : (
                    <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 text-emerald-600" />
                  )}
                  <div>
                    <div className="font-medium text-[var(--color-text)]">{event.label}</div>
                    {event.detail && <div>{event.detail}</div>}
                  </div>
                </div>
              ))}
            </div>
          </details>
        )}
        {!isUser && hasCharts && (
          <p className="mt-3 text-xs text-[var(--color-text-secondary)]">
            Charts for this answer are available in the canvas.
          </p>
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
