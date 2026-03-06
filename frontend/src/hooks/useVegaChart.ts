import { useEffect, useMemo, useRef, useState } from 'react';
import embed, { type Result } from 'vega-embed';
import type { VegaLiteSpec } from '../types';

const MIN_CHART_RENDER_WIDTH = 760;
const FACET_LABEL_GUTTER = 220;
const MIN_FACET_CELL_WIDTH = 560;

export function useVegaChart(spec: VegaLiteSpec | null) {
  const containerRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<Result | null>(null);
  const [containerWidth, setContainerWidth] = useState(0);

  const isFaceted = useMemo(
    () => Boolean(spec && ('facet' in spec || 'concat' in spec || 'hconcat' in spec || 'vconcat' in spec)),
    [spec],
  );
  const renderWidth =
    containerWidth > 0
      ? Math.max(containerWidth, MIN_CHART_RENDER_WIDTH)
      : MIN_CHART_RENDER_WIDTH;

  useEffect(() => {
    if (!spec || !containerRef.current) return;

    const el = containerRef.current;
    const measure = () => {
      const nextWidth = Math.max(0, Math.floor(el.clientWidth));
      setContainerWidth((prev) => (Math.abs(prev - nextWidth) >= 2 ? nextWidth : prev));
    };

    measure();

    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', measure);
      return () => window.removeEventListener('resize', measure);
    }

    const observer = new ResizeObserver(() => {
      measure();
    });
    observer.observe(el);

    return () => {
      observer.disconnect();
    };
  }, [spec]);

  useEffect(() => {
    if (!containerRef.current || !spec) return;

    // Strip title from spec — ChartCard renders its own header
    const specWithoutTitle = { ...spec };
    delete specWithoutTitle.title;

    let fullSpec: Record<string, unknown>;

    if (isFaceted) {
      // Faceted specs do not support fit-x well, so keep each cell wide enough
      // to preserve labels and let the card scroll when the panel is narrow.
      const cellWidth = Math.max(MIN_FACET_CELL_WIDTH, renderWidth - FACET_LABEL_GUTTER);
      fullSpec = {
        ...specWithoutTitle,
        width: cellWidth,
        autosize: { type: 'pad' as const, contains: 'padding' as const },
      };
    } else {
      fullSpec = {
        ...specWithoutTitle,
        width: renderWidth,
        autosize: { type: 'pad' as const, contains: 'padding' as const },
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
        } else {
          result.finalize();
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
  }, [spec, isFaceted, renderWidth]);

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
