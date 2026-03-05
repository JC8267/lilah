import ReactMarkdown from 'react-markdown';
import { Bot, CheckCircle2, Loader2, XCircle } from 'lucide-react';
import { useChatStore } from '../../stores/chat-store';

const TOOL_LABELS: Record<string, string> = {
  quick_insight: 'Analyzing survey data',
  query_data: 'Querying the database',
  create_chart: 'Building visualization',
  demographic_breakout: 'Building demographic breakout',
  question_by_demographic: 'Comparing by demographic',
  question_group_by_demographic: 'Comparing item matrix',
};

function friendlyToolLabel(tool: string): string {
  return TOOL_LABELS[tool] || tool.replace(/_/g, ' ');
}

export function StreamingText() {
  const streamingText = useChatStore((s) => s.streamingText);
  const streamingFilters = useChatStore((s) => s.streamingFilters);
  const streamingToolEvents = useChatStore((s) => s.streamingToolEvents);

  return (
    <div className="flex gap-3">
      <div className="w-8 h-8 rounded-full bg-[var(--color-primary-light)] flex items-center justify-center shrink-0">
        <Bot className="w-4 h-4 text-[var(--color-primary)]" />
      </div>
      <div className="max-w-[80%] rounded-2xl px-4 py-3 text-sm leading-relaxed bg-[var(--color-surface-alt)] text-[var(--color-text)]">
        {streamingFilters.length > 0 && (
          <div className="mb-3 flex flex-wrap items-center gap-2">
            {streamingFilters.map((filter) => (
              <span
                key={`${filter.demo_id}:${filter.demo_level}`}
                className="inline-flex items-center rounded-full bg-white px-2 py-1 text-[11px] font-medium text-[var(--color-primary)]"
              >
                {filter.demo_level.replace(/^(TOTAL|CUSTOMER|PROSPECT): /, '')}
              </span>
            ))}
          </div>
        )}
        {streamingToolEvents.length > 0 && (
          <div className="mb-3 space-y-1.5 border-b border-[var(--color-border)]/70 pb-3 text-xs text-[var(--color-text-secondary)]">
            {streamingToolEvents.map((event, index) => (
              <div key={`${event.label}-${index}`} className="flex items-start gap-2">
                {event.status === 'running' ? (
                  <Loader2 className="mt-0.5 h-3.5 w-3.5 animate-spin text-[var(--color-primary)]" />
                ) : event.status === 'error' ? (
                  <XCircle className="mt-0.5 h-3.5 w-3.5 text-red-500" />
                ) : (
                  <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 text-emerald-600" />
                )}
                <div>
                  <div className="font-medium text-[var(--color-text)]">
                    {event.kind === 'tool' ? friendlyToolLabel(event.label) : event.label}
                  </div>
                  {event.detail && <div>{event.detail}</div>}
                </div>
              </div>
            ))}
          </div>
        )}
        {streamingText ? (
          <div className="prose prose-sm max-w-none [&_p]:my-1 [&_ul]:my-1 [&_li]:my-0.5">
            <ReactMarkdown>{streamingText}</ReactMarkdown>
            <span className="inline-block w-0.5 h-4 rounded-full bg-[var(--color-primary)] animate-pulse ml-0.5" />
          </div>
        ) : (
          streamingToolEvents.length === 0 && (
            <div className="flex items-center gap-2">
              <Loader2 className="w-4 h-4 animate-spin text-[var(--color-primary)]" />
              <span className="text-[var(--color-text-secondary)]">Thinking...</span>
            </div>
          )
        )}
      </div>
    </div>
  );
}
