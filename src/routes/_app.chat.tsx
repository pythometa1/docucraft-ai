import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { Send, Sparkles, Plus, MessageSquare, Bot, User as UserIcon, FileText, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { api } from "@/lib/api";
import { useStore } from "@/lib/store";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/_app/chat")({
  head: () => ({
    meta: [
      { title: "Chat — DocuMind AI" },
      { name: "description", content: "Ask questions about a project's source files. Answers are grounded in the uploaded documents and cite the chunks they came from." },
    ],
  }),
  component: ChatPage,
});

type Msg = { id: string; role: "user" | "assistant"; text: string; sources?: { chunk_id: string; heading_path: string | null }[] };

function ChatPage() {
  const projects = useStore((s) => s.projects);
  const loadProjects = useStore((s) => s.loadProjects);

  const [projectId, setProjectId] = useState("");
  const [conversations, setConversations] = useState<any[]>([]);
  const [conversationId, setConversationId] = useState("");
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // Swallowed, this leaves the project picker empty and the screen looks like
    // an account with no projects rather than a request that failed.
    loadProjects().catch((e: any) =>
      toast.error("Could not load projects", { description: e?.message ?? String(e) }),
    );
  }, []);
  useEffect(() => {
    if (!projectId && projects.length) setProjectId(projects[0].id);
  }, [projects, projectId]);

  useEffect(() => {
    if (!projectId) return;
    api.listConversations(projectId)
      .then((r) => {
        setConversations(r.items);
        setConversationId(r.items[0]?.id ?? "");
        if (!r.items.length) setMessages([]);
      })
      .catch((e: any) => toast.error("Could not load conversations", { description: e?.message ?? String(e) }));
  }, [projectId]);

  useEffect(() => {
    if (!conversationId) return;
    api.listMessages(conversationId)
      .then((r) => setMessages(r.items.map((m: any) => ({ id: m.id, role: m.role, text: m.text, sources: m.sources }))))
      .catch(() => setMessages([]));
  }, [conversationId]);

  useEffect(() => { bottom.current?.scrollIntoView({ behavior: "smooth" }); }, [messages]);

  const newConversation = async () => {
    if (!projectId) return;
    try {
      const c = await api.createConversation(projectId, "New conversation");
      setConversations((prev) => [c, ...prev]);
      setConversationId(c.id);
      setMessages([]);
    } catch (e: any) {
      toast.error("Could not start a conversation", { description: e?.message ?? String(e) });
    }
  };

  const send = async () => {
    const text = input.trim();
    if (!text || sending) return;

    let target = conversationId;
    if (!target) {
      try {
        const c = await api.createConversation(projectId, text.slice(0, 60));
        setConversations((prev) => [c, ...prev]);
        setConversationId(c.id);
        target = c.id;
      } catch (e: any) {
        toast.error("Could not start a conversation", { description: e?.message ?? String(e) });
        return;
      }
    }

    setInput("");
    setSending(true);
    // Show the question immediately; the server assigns the real id when it echoes back.
    setMessages((m) => [...m, { id: `local-${Date.now()}`, role: "user", text }]);
    try {
      const reply = await api.sendMessage(target, text);
      setMessages((m) => [...m, { id: reply.id, role: "assistant", text: reply.text, sources: reply.sources }]);
    } catch (e: any) {
      toast.error("Could not send", { description: e?.message ?? String(e) });
    } finally {
      setSending(false);
    }
  };

  const project = projects.find((p) => p.id === projectId);

  return (
    <div className="flex h-[calc(100vh-4rem)]">
      <aside className="hidden lg:flex w-72 flex-col border-r border-border bg-card/40">
        <div className="p-4 border-b border-border space-y-2">
          <Select value={projectId} onValueChange={setProjectId}>
            <SelectTrigger><SelectValue placeholder="Choose a project" /></SelectTrigger>
            <SelectContent>
              {projects.map((p) => <SelectItem key={p.id} value={p.id}>{p.name}</SelectItem>)}
            </SelectContent>
          </Select>
          <Button className="w-full gap-2" size="sm" onClick={newConversation} disabled={!projectId}>
            <Plus className="h-4 w-4" /> New chat
          </Button>
        </div>
        <div className="flex-1 overflow-y-auto p-2 space-y-1">
          {conversations.map((c) => (
            <button
              key={c.id}
              onClick={() => setConversationId(c.id)}
              className={cn(
                "w-full text-left px-3 py-2.5 rounded-lg transition-colors",
                c.id === conversationId ? "bg-accent text-accent-foreground" : "hover:bg-muted/50",
              )}
            >
              <div className="flex items-start gap-2">
                <MessageSquare className="h-4 w-4 mt-0.5 shrink-0 text-muted-foreground" />
                <div className="min-w-0 flex-1">
                  <div className="text-sm font-medium truncate">{c.title}</div>
                  <div className="text-xs text-muted-foreground mt-0.5">
                    {c.updated_at ? new Date(c.updated_at).toLocaleDateString() : ""}
                  </div>
                </div>
              </div>
            </button>
          ))}
          {conversations.length === 0 && (
            <p className="text-xs text-muted-foreground p-3">No conversations yet.</p>
          )}
        </div>
      </aside>

      <div className="flex-1 flex flex-col">
        <header className="border-b border-border px-6 py-4 flex items-center justify-between">
          <div>
            <h1 className="text-lg font-semibold">{project?.name ?? "Chat"}</h1>
            <p className="text-xs text-muted-foreground mt-0.5">
              Grounded in this project's uploaded source files
            </p>
          </div>
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <Sparkles className="h-4 w-4 text-primary" /> Retrieval-grounded
          </div>
        </header>

        <div className="flex-1 overflow-y-auto px-6 py-6 space-y-6">
          {messages.length === 0 && (
            <div className="text-center text-sm text-muted-foreground pt-10">
              Ask something about this project's sources. Every answer cites the chunks it came from.
            </div>
          )}
          {messages.map((m) => (
            <div key={m.id} className={cn("flex gap-3 max-w-3xl", m.role === "user" && "ml-auto flex-row-reverse")}>
              <div className={cn(
                "h-8 w-8 rounded-lg flex items-center justify-center shrink-0",
                m.role === "assistant" ? "bg-gradient-to-br from-primary to-purple-500 text-white" : "bg-muted text-foreground",
              )}>
                {m.role === "assistant" ? <Bot className="h-4 w-4" /> : <UserIcon className="h-4 w-4" />}
              </div>
              <div className={cn("flex-1 min-w-0", m.role === "user" && "text-right")}>
                <div className={cn(
                  "inline-block rounded-2xl px-4 py-3 text-sm leading-relaxed",
                  m.role === "assistant" ? "bg-card border border-border text-left" : "bg-primary text-primary-foreground",
                )}>
                  {m.text}
                </div>
                {m.sources && m.sources.length > 0 && (
                  <div className="mt-2 flex flex-wrap gap-2">
                    {m.sources.map((s) => (
                      <span key={s.chunk_id} className="inline-flex items-center gap-1.5 text-xs px-2 py-1 rounded-md bg-muted text-muted-foreground border border-border">
                        <FileText className="h-3 w-3" /> {s.heading_path || s.chunk_id.slice(0, 8)}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            </div>
          ))}
          {sending && (
            <div className="flex gap-3 max-w-3xl text-sm text-muted-foreground items-center">
              <Loader2 className="h-4 w-4 animate-spin" /> Searching the sources…
            </div>
          )}
          <div ref={bottom} />
        </div>

        <div className="border-t border-border p-4">
          <div className="max-w-3xl mx-auto flex items-end gap-2 rounded-2xl border border-border bg-card p-2 shadow-sm">
            <Input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && send()}
              disabled={!projectId || sending}
              placeholder={projectId ? "Ask about this project's sources…" : "Choose a project first"}
              className="border-0 focus-visible:ring-0 shadow-none bg-transparent"
            />
            <Button size="icon" className="h-9 w-9 shrink-0" onClick={send} disabled={!projectId || sending}>
              <Send className="h-4 w-4" />
            </Button>
          </div>
          <p className="text-[11px] text-muted-foreground text-center mt-2">
            Answers are grounded in retrieved source chunks. Verify before approving a draft.
          </p>
        </div>
      </div>
    </div>
  );
}
