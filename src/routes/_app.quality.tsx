import { createFileRoute, redirect } from "@tanstack/react-router";

// The route stays so old bookmarks land somewhere useful; the page itself is gone.
export const Route = createFileRoute("/_app/quality")({
  beforeLoad: () => {
    throw redirect({ to: "/dashboard", replace: true });
  },
});
