# University of Haripur FYP Portal — Frontend

React/Vite frontend for the Student, Supervisor, and Admin workspaces.

## Local development

Copy `.env.example` to `.env`, provide the deployed API URL and public Supabase values, then run `npm install` and `npm run dev`.

## Production

Run `npm run build`. Netlify publishes `dist`; `public/_redirects` provides the SPA history fallback. Configure `VITE_API_BASE_URL` in the Netlify environment before building.

Never expose a Supabase service-role key or backend credentials through a `VITE_` variable.
