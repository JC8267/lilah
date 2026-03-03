import { Header } from './Header';
import { Sidebar } from './Sidebar';
import { ChatPanel } from '../chat/ChatPanel';
import { CanvasPanel } from '../canvas/CanvasPanel';
import { useChatStore } from '../../stores/chat-store';

export function AppShell() {
  const messages = useChatStore((s) => s.messages);
  const streamingCharts = useChatStore((s) => s.streamingCharts);

  // Show canvas if there are any charts in messages or streaming
  const hasCharts =
    streamingCharts.length > 0 ||
    messages.some((m) => m.charts && m.charts.length > 0);

  return (
    <div className="h-screen flex flex-col">
      <Header />
      <div className="flex flex-1 overflow-hidden">
        <Sidebar />
        <main className="flex-1 flex">
          <ChatPanel className={`transition-all duration-300 ease-in-out ${hasCharts ? 'w-1/2 border-r border-[var(--color-border)]' : 'w-full'}`} />
          {hasCharts && <CanvasPanel className="w-1/2" />}
        </main>
      </div>
    </div>
  );
}
