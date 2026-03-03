import ReactMarkdown from 'react-markdown';
import { Bot, Loader2 } from 'lucide-react';
import { useChatStore } from '../../stores/chat-store';

const TOOL_LABELS: Record<string, string> = {
  quick_insight: 'Analyzing survey data',
  run_query: 'Querying the database',
  create_chart: 'Building visualization',
  summarize: 'Summarizing results',
};

function friendlyToolLabel(tool: string): string {
  return TOOL_LABELS[tool] || tool.replace(/_/g, ' ');
}

export function StreamingText() {
  const streamingText = useChatStore((s) => s.streamingText);
  const toolStatus = useChatStore((s) => s.toolStatus);

  return (
    <div className="flex gap-3">
      <div className="w-8 h-8 rounded-full bg-[var(--color-primary-light)] flex items-center justify-center shrink-0">
        <Bot className="w-4 h-4 text-[var(--color-primary)]" />
      </div>
      <div className="max-w-[80%] rounded-2xl px-4 py-3 text-sm leading-relaxed bg-[var(--color-surface-alt)] text-[var(--color-text)]">
        {toolStatus && (
          <div className="flex items-center gap-2 text-xs text-[var(--color-text-secondary)] mb-2">
            <Loader2
              className={`w-3 h-3 ${toolStatus.status === 'running' ? 'animate-spin' : ''}`}
            />
            <span>
              {toolStatus.status === 'running'
                ? `${friendlyToolLabel(toolStatus.tool)}…`
                : `${friendlyToolLabel(toolStatus.tool)} complete`}
            </span>
          </div>
        )}
        {streamingText ? (
          <div className="prose prose-sm max-w-none [&_p]:my-1 [&_ul]:my-1 [&_li]:my-0.5">
            <ReactMarkdown>{streamingText}</ReactMarkdown>
            <span className="inline-block w-0.5 h-4 rounded-full bg-[var(--color-primary)] animate-pulse ml-0.5" />
          </div>
        ) : (
          !toolStatus && (
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
