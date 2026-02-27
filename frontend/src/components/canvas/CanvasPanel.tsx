import { useRef, useEffect } from 'react';
import { useChatStore } from '../../stores/chat-store';
import { ChartCard } from './ChartCard';
import { InsightCard } from './InsightCard';
import type { VegaLiteSpec } from '../../types';

interface CanvasPanelProps {
  className?: string;
}

export function CanvasPanel({ className = '' }: CanvasPanelProps) {
  const messages = useChatStore((s) => s.messages);
  const streamingCharts = useChatStore((s) => s.streamingCharts);
  const isStreaming = useChatStore((s) => s.isStreaming);
  const bottomRef = useRef<HTMLDivElement>(null);

  // Collect chart + insight pairs grouped by message
  const groups: { charts: VegaLiteSpec[]; insight?: string; key: string }[] = [];

  for (const msg of messages) {
    if (msg.role === 'assistant' && (msg.charts?.length || msg.content)) {
      groups.push({
        charts: msg.charts || [],
        insight: msg.content || undefined,
        key: msg.id,
      });
    }
  }

  // Add streaming charts
  if (isStreaming && streamingCharts.length > 0) {
    groups.push({
      charts: streamingCharts,
      key: 'streaming',
    });
  }

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [groups.length]);

  return (
    <div className={`flex flex-col h-full overflow-y-auto bg-[var(--color-surface-alt)] ${className}`}>
      <div className="p-5 space-y-6">
        {groups.map((group, gi) => (
          <div key={group.key} className="space-y-4">
            {group.charts.map((chart, ci) => (
              <ChartCard key={`${group.key}-chart-${ci}`} spec={chart} index={gi * 10 + ci} />
            ))}
            {group.insight && <InsightCard text={group.insight} />}
          </div>
        ))}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
