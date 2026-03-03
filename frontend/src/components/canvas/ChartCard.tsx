import { useVegaChart } from '../../hooks/useVegaChart';
import { ExportMenu } from './ExportMenu';
import type { VegaLiteSpec } from '../../types';

interface ChartCardProps {
  spec: VegaLiteSpec;
  index: number;
}

export function ChartCard({ spec, index }: ChartCardProps) {
  const { containerRef, exportPNG, exportSVG } = useVegaChart(spec);

  const specTitle = spec.title as unknown;
  let title = `Chart ${index + 1}`;
  let subtitle: string | undefined;

  if (typeof specTitle === 'string' && specTitle.trim()) {
    title = specTitle.trim();
  } else if (
    typeof specTitle === 'object' &&
    specTitle !== null
  ) {
    const obj = specTitle as { text?: string; subtitle?: string };
    if (typeof obj.text === 'string' && obj.text.trim()) {
      title = obj.text.trim();
    }
    if (typeof obj.subtitle === 'string' && obj.subtitle.trim()) {
      subtitle = obj.subtitle.trim();
    }
  }

  return (
    <div className="border border-[var(--color-border)] rounded-xl bg-[var(--color-surface)] overflow-hidden shadow-sm">
      <div className="flex items-center justify-between px-5 py-3 border-b border-[var(--color-border)] bg-[var(--color-surface)]">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold text-[var(--color-text)] truncate">
            {title}
          </h3>
          {subtitle && (
            <p className="text-xs text-[var(--color-text-secondary)] truncate mt-0.5">
              {subtitle}
            </p>
          )}
        </div>
        <ExportMenu onExportPNG={exportPNG} onExportSVG={exportSVG} />
      </div>
      <div
        ref={containerRef}
        className="chart-container px-4 py-5 w-full min-h-[280px] overflow-x-auto"
      />
    </div>
  );
}
