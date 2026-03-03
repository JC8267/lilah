import { Download } from 'lucide-react';

interface ExportMenuProps {
  onExportPNG: () => Promise<string | null>;
  onExportSVG: () => Promise<string | null>;
}

function downloadDataUrl(dataUrl: string, filename: string) {
  const a = document.createElement('a');
  a.href = dataUrl;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

export function ExportMenu({ onExportPNG, onExportSVG }: ExportMenuProps) {
  const handlePNG = async () => {
    const url = await onExportPNG();
    if (url) downloadDataUrl(url, 'chart.png');
  };

  const handleSVG = async () => {
    const svg = await onExportSVG();
    if (svg) {
      const blob = new Blob([svg], { type: 'image/svg+xml' });
      const url = URL.createObjectURL(blob);
      downloadDataUrl(url, 'chart.svg');
      URL.revokeObjectURL(url);
    }
  };

  return (
    <div className="flex items-center gap-0.5 shrink-0 ml-3">
      <button
        onClick={handlePNG}
        className="text-xs px-2 py-1.5 rounded-md border border-[var(--color-border)] hover:bg-gray-100 text-[var(--color-text-secondary)] flex items-center gap-1.5 transition-colors"
        title="Export as high-resolution PNG"
      >
        <Download className="w-3.5 h-3.5" />
        PNG
      </button>
      <button
        onClick={handleSVG}
        className="text-xs px-2 py-1.5 rounded-md border border-[var(--color-border)] hover:bg-gray-100 text-[var(--color-text-secondary)] flex items-center gap-1.5 transition-colors"
        title="Export as SVG"
      >
        <Download className="w-3.5 h-3.5" />
        SVG
      </button>
    </div>
  );
}
