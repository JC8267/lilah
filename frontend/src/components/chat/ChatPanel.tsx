import { useRef, useEffect } from 'react';
import { BarChart3 } from 'lucide-react';
import { useChatStore } from '../../stores/chat-store';
import { useChat } from '../../hooks/useChat';
import { MessageBubble } from './MessageBubble';
import { StreamingText } from './StreamingText';
import { ChatInput } from './ChatInput';

interface ChatPanelProps {
  className?: string;
}

export function ChatPanel({ className = '' }: ChatPanelProps) {
  const messages = useChatStore((s) => s.messages);
  const isStreaming = useChatStore((s) => s.isStreaming);
  const { sendMessage } = useChat();
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, isStreaming]);

  return (
    <div className={`flex flex-col h-full ${className}`}>
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {messages.length === 0 && !isStreaming && (
          <div className="flex flex-col items-center justify-center h-full text-center">
            <BarChart3 className="w-12 h-12 text-[var(--color-primary)] mb-4 opacity-40" />
            <h2 className="text-xl font-semibold text-[var(--color-text)] mb-2">
              Ask about the survey
            </h2>
            <p className="text-sm text-[var(--color-text-secondary)] max-w-md">
              Explore IKEA home furnishing survey data from 24K respondents. Ask questions
              about home types, furniture ownership, purchase plans, demographics, and more.
            </p>
            <div className="mt-6 grid grid-cols-1 sm:grid-cols-2 gap-2 max-w-lg">
              {[
                'What types of homes do people live in?',
                'How does furniture ownership differ by income?',
                'What are the top planned purchases for kitchens?',
                'Compare renters vs homeowners on room satisfaction',
              ].map((q) => (
                <button
                  key={q}
                  onClick={() => sendMessage(q)}
                  className="text-left text-xs px-3 py-2 rounded-lg border border-[var(--color-border)] hover:bg-[var(--color-surface-alt)] transition-colors text-[var(--color-text-secondary)]"
                >
                  {q}
                </button>
              ))}
            </div>
          </div>
        )}

        <div className="max-w-3xl mx-auto space-y-4">
          {messages.map((msg) => (
            <MessageBubble key={msg.id} message={msg} />
          ))}
          {isStreaming && <StreamingText />}
          <div ref={bottomRef} />
        </div>
      </div>

      <ChatInput onSend={sendMessage} />
    </div>
  );
}
