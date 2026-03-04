import { useEffect, useRef } from 'react';
import embed, { type Result } from 'vega-embed';
import type { VegaLiteSpec } from '../types';

export function useVegaChart(spec: VegaLiteSpec | null) {
  const containerRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<Result | null>(null);

  useEffect(() => {
    if (!containerRef.current || !spec) return;

    // Don't force width: 'container' on faceted/concat specs — it breaks layout.
    const isFaceted = 'facet' in spec || 'concat' in spec || 'hconcat' in spec || 'vconcat' in spec;

    // Strip title from spec — ChartCard renders its own header
    const { title: _title, ...specWithoutTitle } = spec;

    let fullSpec: Record<string, unknown>;

    if (isFaceted) {
      // Vega-Lite doesn't support autosize on faceted/concat specs, so we
      // measure the container and set an explicit cell width instead.
      const cw = containerRef.current.clientWidth;
      // Reserve ~260px for y-axis labels, facet headers, and padding.
      const cellWidth = cw > 0 ? Math.max(150, cw - 260) : 350;
      fullSpec = { ...specWithoutTitle, width: cellWidth };
    } else {
      fullSpec = {
        ...specWithoutTitle,
        width: 'container' as const,
        autosize: { type: 'fit-x' as const, contains: 'padding' as const },
      };
    }

    let cancelled = false;

    embed(containerRef.current, fullSpec as never, {
      actions: false,
      renderer: 'svg',
      // Prevent unnecessary tooltip jitter
      tooltip: { theme: 'custom' },
    })
      .then((result) => {
        if (!cancelled) {
          viewRef.current = result;
        }
      })
      .catch(() => {
        // Spec may be invalid during streaming; ignore
      });

    return () => {
      cancelled = true;
      if (viewRef.current) {
        viewRef.current.finalize();
        viewRef.current = null;
      }
    };
  }, [spec]);

  const exportPNG = async (): Promise<string | null> => {
    if (!viewRef.current) return null;

    // Use adaptive high-resolution export so charts from the half-width panel
    // don't look soft when opened full-screen.
    const view = viewRef.current.view;
    const width = Math.max(1, Number(view.width() || 0));
    const height = Math.max(1, Number(view.height() || 0));
    const longEdge = Math.max(width, height);

    // Target ~2200px long edge while keeping memory usage reasonable.
    const adaptiveScale = Math.ceil(2200 / longEdge);
    const scale = Math.max(2, Math.min(6, adaptiveScale));

    return view.toImageURL('png', scale);
  };

  const exportSVG = async (): Promise<string | null> => {
    if (!viewRef.current) return null;
    return viewRef.current.view.toSVG();
  };

  return { containerRef, exportPNG, exportSVG };
}
