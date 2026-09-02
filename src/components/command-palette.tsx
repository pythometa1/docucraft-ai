/**
 * ⌘K, which the header has been promising since it was written.
 *
 * There was a search input in the header with no handler and a ⌘K badge beside
 * it, and `cmdk` plus `ui/command.tsx` were already installed and imported by
 * nothing. So the parts were all here; what was missing was the twenty lines
 * that ask the server.
 *
 * It searches what the API can actually search -- projects by name, and the
 * templates inside the project you are looking at -- rather than presenting a
 * box that implies full-text search over document bodies, which no endpoint
 * offers. Promising less and delivering it is the whole point of the control.
 */

import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { FileText, FolderKanban, Loader2 } from "lucide-react";

import { api } from "@/lib/api";
import {
  CommandDialog, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList,
} from "@/components/ui/command";

type Hit =
  | { kind: "project"; id: string; name: string; sub: string }
  | { kind: "template"; id: string; name: string; sub: string };

export function CommandPalette({ open, onOpenChange }: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
}) {
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<Hit[]>([]);
  const [loading, setLoading] = useState(false);

  // Debounced, and guarded against out-of-order responses. Typing "offer" fires
  // five requests and the third can land after the fifth; without the sequence
  // check the list would settle on results for "off".
  useEffect(() => {
    if (!open) return;
    let live = true;
    setLoading(true);
    const timer = setTimeout(() => {
      api.listProjects(query || undefined, { limit: 8 })
        .then(async (res) => {
          if (!live) return;
          const projects: Hit[] = (res.items ?? []).map((p: any) => ({
            kind: "project" as const,
            id: p.id,
            name: p.name,
            sub: [p.region, p.function].filter(Boolean).join(" · "),
          }));
          setHits(projects);
        })
        .catch(() => { if (live) setHits([]); })
        .finally(() => { if (live) setLoading(false); });
    }, 200);
    return () => { live = false; clearTimeout(timer); };
  }, [query, open]);

  // Cleared on close rather than on open, so reopening does not flash the
  // previous search before the new one lands.
  useEffect(() => { if (!open) { setQuery(""); setHits([]); } }, [open]);

  const projects = useMemo(() => hits.filter((h) => h.kind === "project"), [hits]);

  const go = (to: () => void) => { onOpenChange(false); to(); };

  return (
    <CommandDialog open={open} onOpenChange={onOpenChange}>
      <CommandInput
        placeholder="Search projects…"
        value={query}
        onValueChange={setQuery}
      />
      <CommandList>
        {loading && (
          <div className="flex items-center gap-2 px-4 py-6 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" /> Searching…
          </div>
        )}
        {!loading && <CommandEmpty>Nothing matches that.</CommandEmpty>}

        {projects.length > 0 && (
          <CommandGroup heading="Projects">
            {projects.map((p) => (
              <CommandItem
                key={p.id}
                value={`${p.name} ${p.sub}`}
                onSelect={() => go(() => navigate({ to: "/projects/$id", params: { id: p.id } }))}
              >
                <FolderKanban className="mr-2 h-4 w-4 text-brand" />
                <span className="flex-1 truncate">{p.name}</span>
                <span className="ml-2 shrink-0 text-xs text-muted-foreground">{p.sub}</span>
              </CommandItem>
            ))}
          </CommandGroup>
        )}

        <CommandGroup heading="Go to">
          {[
            { label: "Projects", to: "/dashboard" as const, icon: FolderKanban },
            { label: "Templates", to: "/templates" as const, icon: FileText },
            { label: "Review queue", to: "/review" as const, icon: FileText },
          ].map((item) => (
            <CommandItem
              key={item.to}
              value={item.label}
              onSelect={() => go(() => navigate({ to: item.to }))}
            >
              <item.icon className="mr-2 h-4 w-4 text-muted-foreground" />
              {item.label}
            </CommandItem>
          ))}
        </CommandGroup>
      </CommandList>
    </CommandDialog>
  );
}
