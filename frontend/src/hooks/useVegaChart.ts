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

    const fullSpec = {
      ...spec,
      ...(isFaceted ? {} : {
        width: 'container' as const,
        autosize: { type: 'fit' as const, contains: 'padding' as const },
      }),
    };

    let cancelled = false;

    embed(containerRef.current, fullSpec as never, {
      actions: false,
      renderer: 'svg',
      // Prevent unnecessary tooltip jitter
      tooltip: { theme: 'custom' },
    }).then((result) => {
      if (!cancelled) {
        viewRef.current = result;
      }
    }).catch(() => {
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
    return viewRef.current.view.toImageURL('png', 2); // 2x for retina
  };

  const exportSVG = async (): Promise<string | null> => {
    if (!viewRef.current) return null;
    return viewRef.current.view.toSVG();
  };

  return { containerRef, exportPNG, exportSVG };
}
