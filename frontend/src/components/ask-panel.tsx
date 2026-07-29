"use client";

import { useState, useTransition } from "react";

import { askQuestion, type Source } from "@/app/insights/actions";

/**
 * The natural-language panel.
 *
 * Two decisions here are about honesty rather than layout.
 *
 * 1. **The sources are shown, not hidden behind a tooltip.** Every figure in
 *    the answer came from a named query over a stated date range, and being
 *    able to see which one is what separates a checkable answer from a
 *    confident one. It is collapsed by default because most readers will not
 *    want it, and one click away because some will.
 *
 * 2. **A rejected answer shows an error, not the answer with a warning.** The
 *    backend discards any answer containing a figure it cannot trace to a
 *    query. Rendering the text anyway with a caution badge would defeat the
 *    point: the number is what people remember, and the badge is not.
 */
export function AskPanel({ suggestions }: { suggestions: string[] }) {
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState<string | null>(null);
  const [sources, setSources] = useState<Source[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  function ask(text: string) {
    setQuestion(text);
    setAnswer(null);
    setError(null);
    setSources([]);

    startTransition(async () => {
      const result = await askQuestion(text);
      if (result.ok) {
        setAnswer(result.answer);
        setSources(result.sources);
      } else {
        setError(result.error);
      }
    });
  }

  return (
    <section
      aria-labelledby="ask-heading"
      className="flex flex-col gap-4 rounded-lg border border-zinc-200 p-5 dark:border-zinc-800"
    >
      <div>
        <h2 id="ask-heading" className="text-sm font-medium uppercase tracking-wide text-zinc-500">
          Ask about your money
        </h2>
        <p className="mt-1 text-xs text-zinc-500">
          Answers are computed by queries against your own data. Any figure
          that cannot be traced back to one is discarded rather than shown.
        </p>
      </div>

      <form
        onSubmit={(event) => {
          event.preventDefault();
          ask(question);
        }}
        className="flex flex-col gap-2 sm:flex-row"
      >
        <label htmlFor="question" className="sr-only">
          Your question
        </label>
        <input
          id="question"
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          maxLength={500}
          placeholder="Why did I spend more this month?"
          className="flex-1 rounded border border-zinc-300 bg-transparent px-3 py-2 text-sm dark:border-zinc-700"
        />
        <button
          type="submit"
          disabled={pending || question.trim().length === 0}
          className="rounded bg-zinc-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900"
        >
          {pending ? "Thinking…" : "Ask"}
        </button>
      </form>

      {!answer && !error && !pending && (
        <ul className="flex flex-wrap gap-2">
          {suggestions.map((suggestion) => (
            <li key={suggestion}>
              <button
                type="button"
                onClick={() => ask(suggestion)}
                className="rounded-full border border-zinc-300 px-3 py-1 text-xs text-zinc-600 hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-400 dark:hover:bg-zinc-900"
              >
                {suggestion}
              </button>
            </li>
          ))}
        </ul>
      )}

      {/* aria-live so a screen reader announces the answer when it lands --
          without it the panel updates silently and the user has to go
          looking for the change. */}
      <div aria-live="polite" className="flex flex-col gap-3">
        {error && (
          <p className="rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200">
            {error}
          </p>
        )}

        {answer && (
          <>
            <p className="whitespace-pre-wrap text-sm leading-relaxed">{answer}</p>

            {sources.length > 0 && (
              <details className="text-xs text-zinc-500">
                <summary className="cursor-pointer">
                  Where these numbers came from ({sources.length}{" "}
                  {sources.length === 1 ? "query" : "queries"})
                </summary>
                <ul className="mt-2 flex flex-col gap-1 font-mono">
                  {sources.map((source, index) => (
                    <li key={`${source.tool}-${index}`}>
                      {source.tool}(
                      {Object.entries(source.arguments)
                        .map(([key, value]) => `${key}=${String(value)}`)
                        .join(", ")}
                      )
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </>
        )}
      </div>
    </section>
  );
}
