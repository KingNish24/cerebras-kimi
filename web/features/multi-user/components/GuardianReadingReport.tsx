"use client";


import type { GuardianReadingSummary } from "@/lib/guardian-api";

export function GuardianReadingReport({ reading }: { reading: GuardianReadingSummary }) {
  
  const helpEvents = reading.help_events ?? [];
  const titles = new Map(reading.quizzes.map(item => [item.material_id, item.material_title]));
  return (
    <section aria-label={"Reading progress and quiz records"} className="space-y-3 rounded-lg border border-[var(--border)] p-4">
      <h3 className="text-sm font-medium">{"Reading progress and quiz records"}</h3>
      <p className="text-xs text-[var(--muted-foreground)]">{"Reading position and quiz results do not establish independent mastery."}</p>
      {reading.reading_status === "available" ? <>
      <p className="text-xs">{`Showing ${reading.progress.length} of ${reading.progress_total} reading records`}</p>
      <ul className="space-y-2 text-sm">
        {reading.progress.map(item => (
          <li key={item.material_id}>
            <span>{item.material_title || titles.get(item.material_id) || `Reading material ${item.material_id}`}</span>{" · "}
            {`Current section ${item.latest_locator} · furthest position ${Math.round(item.furthest_percentage * 100)}%`}
          </li>
        ))}
      </ul>
      <p className="text-xs text-[var(--muted-foreground)]">{`Recent reading activities: ${reading.activities.length} (up to ${reading.limit})`}</p>
      </> : <p className="text-sm">{reading.reading_status === "migration_required" ? "Reading records need migration before they can be reported. Viewing this report does not migrate them." : "Reading records have not been initialized."}</p>}
      {reading.quiz_status === "available" ? <>
      <p className="text-xs">{`Showing ${reading.quizzes.length} of ${reading.quiz_total} quiz records`}</p>
      <ul className="space-y-3 text-sm">
        {reading.quizzes.map(item => (
          <li key={item.id} className="border-t border-[var(--border)] pt-2">
            <p>{item.question}</p>
            <p className="text-xs text-[var(--muted-foreground)]">
              {item.is_correct === null ? "Not graded" : item.is_correct ? "Correct" : "Incorrect"}{" · "}
              {`${item.attempt_count} attempts`}{" · "}
              {item.hints_used > 0 ? `${item.hints_used} recorded hints` : "No question-linked help recorded"}
            </p>
          </li>
        ))}
      </ul>
      </> : <p className="text-sm">{reading.quiz_status === "migration_required" ? "Quiz records need migration before they can be reported. Viewing this report does not migrate them." : "Quiz records have not been initialized."}</p>}
      <h4 className="text-sm font-medium">{"Question-linked help history"}</h4>
      <p className="text-xs text-[var(--muted-foreground)]">{"Help may occur before or after an answer; it does not change earlier attempt records."}</p>
      {reading.help_status === "available" ? <>
        <p className="text-xs">{`Showing ${helpEvents.length} of ${reading.help_total ?? 0} help records`}</p>
        <ul className="space-y-3 text-sm">
          {helpEvents.map((item, index) => (
            <li key={`${item.question_id}:${item.created_at}:${index}`} className="border-t border-[var(--border)] pt-2">
              <p>{item.question}</p>
              <p className="text-xs text-[var(--muted-foreground)]">
                {item.material_title || titles.get(item.material_id) || `Reading material ${item.material_id}`}{" · "}
                {`Reading section ${item.locator}`}{" · "}
                {item.help_kind === "guided_learning:guide" ? "Guide me" : item.help_kind === "vocabulary:explain" ? "Explain vocabulary" : "Hint"}{" · "}
                {new Date(item.created_at * 1000).toLocaleString("en")}
              </p>
            </li>
          ))}
        </ul>
      </> : <p className="text-sm">{"Question-linked help history is unavailable in this version."}</p>}
    </section>
  );
}
