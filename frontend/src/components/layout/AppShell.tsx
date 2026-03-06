import { useEffect, useRef, useState, type KeyboardEvent, type PointerEvent as ReactPointerEvent } from 'react';
import { Header } from './Header';
import { Sidebar } from './Sidebar';
import { ChatPanel } from '../chat/ChatPanel';
import { CanvasPanel } from '../canvas/CanvasPanel';
import { useChatStore } from '../../stores/chat-store';

const MIN_CHAT_PANEL_WIDTH = 420;
const MIN_CANVAS_PANEL_WIDTH = 760;
const DEFAULT_CANVAS_PANEL_WIDTH = 820;
const SPLITTER_WIDTH = 12;
const RESIZE_STEP = 48;

function clampCanvasWidth(nextWidth: number, totalWidth: number) {
  const maxWidth = Math.max(
    MIN_CANVAS_PANEL_WIDTH,
    totalWidth - MIN_CHAT_PANEL_WIDTH - SPLITTER_WIDTH,
  );
  return Math.min(Math.max(nextWidth, MIN_CANVAS_PANEL_WIDTH), maxWidth);
}

export function AppShell() {
  const messages = useChatStore((s) => s.messages);
  const streamingCharts = useChatStore((s) => s.streamingCharts);
  const mainRef = useRef<HTMLElement>(null);
  const [mainWidth, setMainWidth] = useState(0);
  const [canvasWidth, setCanvasWidth] = useState(DEFAULT_CANVAS_PANEL_WIDTH);
  const [isResizing, setIsResizing] = useState(false);

  // Show canvas if there are any charts in messages or streaming
  const hasCharts =
    streamingCharts.length > 0 ||
    messages.some((m) => m.charts && m.charts.length > 0);
  const shouldStackPanels =
    hasCharts &&
    mainWidth > 0 &&
    mainWidth < MIN_CHAT_PANEL_WIDTH + MIN_CANVAS_PANEL_WIDTH + SPLITTER_WIDTH;
  const effectiveCanvasWidth =
    hasCharts && !shouldStackPanels && mainWidth > 0
      ? clampCanvasWidth(canvasWidth, mainWidth)
      : DEFAULT_CANVAS_PANEL_WIDTH;

  useEffect(() => {
    if (!mainRef.current) return;

    const element = mainRef.current;
    const measure = () => {
      setMainWidth(Math.max(0, Math.floor(element.clientWidth)));
    };

    measure();

    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', measure);
      return () => window.removeEventListener('resize', measure);
    }

    const observer = new ResizeObserver(() => {
      measure();
    });
    observer.observe(element);

    return () => {
      observer.disconnect();
    };
  }, []);

  useEffect(() => {
    if (!isResizing || shouldStackPanels) return;

    const handlePointerMove = (event: PointerEvent) => {
      if (!mainRef.current) return;
      const rect = mainRef.current.getBoundingClientRect();
      const nextWidth = rect.right - event.clientX;
      setCanvasWidth(clampCanvasWidth(nextWidth, rect.width));
    };

    const stopResizing = () => {
      setIsResizing(false);
    };

    const previousCursor = document.body.style.cursor;
    const previousUserSelect = document.body.style.userSelect;
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';

    window.addEventListener('pointermove', handlePointerMove);
    window.addEventListener('pointerup', stopResizing);
    window.addEventListener('pointercancel', stopResizing);

    return () => {
      document.body.style.cursor = previousCursor;
      document.body.style.userSelect = previousUserSelect;
      window.removeEventListener('pointermove', handlePointerMove);
      window.removeEventListener('pointerup', stopResizing);
      window.removeEventListener('pointercancel', stopResizing);
    };
  }, [isResizing, shouldStackPanels]);

  const handleResizeStart = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0 || shouldStackPanels) return;
    event.preventDefault();
    setIsResizing(true);
  };

  const handleResizeKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (shouldStackPanels || mainWidth <= 0) return;

    if (event.key === 'ArrowLeft') {
      event.preventDefault();
      setCanvasWidth((prev) => clampCanvasWidth(prev + RESIZE_STEP, mainWidth));
    } else if (event.key === 'ArrowRight') {
      event.preventDefault();
      setCanvasWidth((prev) => clampCanvasWidth(prev - RESIZE_STEP, mainWidth));
    }
  };

  return (
    <div className="h-screen flex flex-col">
      <Header />
      <div className="flex flex-1 overflow-hidden">
        <Sidebar />
        <main
          ref={mainRef}
          className={`flex-1 min-w-0 overflow-hidden ${hasCharts && shouldStackPanels ? 'flex flex-col' : 'flex'}`}
        >
          <div
            className={`min-w-0 min-h-0 ${
              hasCharts && shouldStackPanels
                ? 'basis-[52%] border-b border-[var(--color-border)]'
                : 'flex-1'
            }`}
            style={
              hasCharts && !shouldStackPanels
                ? { minWidth: `${MIN_CHAT_PANEL_WIDTH}px` }
                : undefined
            }
          >
            <ChatPanel className="h-full" />
          </div>

          {hasCharts && !shouldStackPanels && (
            <div
              role="separator"
              aria-label="Resize chart panel"
              aria-orientation="vertical"
              tabIndex={0}
              onPointerDown={handleResizeStart}
              onKeyDown={handleResizeKeyDown}
              className={`group relative z-10 hidden shrink-0 cursor-col-resize touch-none items-stretch justify-center bg-[var(--color-surface-alt)] transition-colors lg:flex ${isResizing ? 'bg-[var(--color-primary-light)]' : ''}`}
              style={{ width: `${SPLITTER_WIDTH}px` }}
            >
              <div className="my-3 w-px rounded-full bg-[var(--color-border)] transition-colors group-hover:bg-[var(--color-primary)] group-focus-visible:bg-[var(--color-primary)]" />
              <div className="pointer-events-none absolute inset-y-0 left-1/2 w-1 -translate-x-1/2 rounded-full bg-transparent group-hover:bg-[var(--color-primary-light)] group-focus-visible:bg-[var(--color-primary-light)]" />
            </div>
          )}

          {hasCharts && (
            <div
              className={`min-w-0 min-h-0 ${
                shouldStackPanels
                  ? 'flex-1'
                  : 'shrink-0 border-l border-[var(--color-border)]'
              }`}
              style={
                shouldStackPanels
                  ? undefined
                  : {
                      width: `${effectiveCanvasWidth}px`,
                      flexBasis: `${effectiveCanvasWidth}px`,
                      minWidth: `${MIN_CANVAS_PANEL_WIDTH}px`,
                    }
              }
            >
              <CanvasPanel className="h-full" />
            </div>
          )}
        </main>
      </div>
    </div>
  );
}
