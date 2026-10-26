import type { Certificate, Item, Post, QuizStanding } from './api'
import { Contents, Progress } from './contents'
import { TrainingGuide } from './guide'
import { BadgeShelf, CertificateReady } from './honors'
import { QuizTrail } from './quiz'
import { Thai } from './ui'

export function Grid({ posts, onOpen, quizzes = [], exempt = false, onQuiz, finalExam, certificate }: {
  posts: Post[]; onOpen: (post: Post, item?: Item) => void
  finalExam?: QuizStanding | null; quizzes?: QuizStanding[]; exempt?: boolean; onQuiz?: (chapter: number) => void
  certificate?: Certificate | null
}) {
  if (!posts.length) {
    return (
      <main className="mx-auto w-full max-w-2xl space-y-6 px-4 py-16">
        <p className="text-neutral-600 dark:text-neutral-300">Nothing has been posted yet.</p>
        {/* A certificate outlives the catalog, so it stays one tap away while nothing is published. */}
        {certificate ? <CertificateReady /> : null}
      </main>
    )
  }
  // Only the SOP chapters are required: the optional Modules neither hold the bar back nor fill it.
  const chapters = posts.filter(post => post.chapter != null)
  return (
    <main className="mx-auto w-full max-w-2xl space-y-4 px-4 py-6">
      <section className="space-y-4 rounded-2xl border border-neutral-200 dark:border-neutral-800 bg-white dark:bg-neutral-900 p-5">
        <h1 className="text-xl font-semibold">Your training</h1>
        <TrainingGuide finalExam={finalExam} certificate={certificate} posts={posts} quizzes={quizzes} exempt={exempt} onOpen={onOpen} onQuiz={onQuiz} />
        {quizzes.length && onQuiz ? <div className="border-t border-neutral-100 dark:border-neutral-800 pt-4"><QuizTrail quizzes={quizzes} exempt={exempt} onQuiz={onQuiz} /></div> : null}
        <div className="space-y-2 border-t border-neutral-100 pt-4 dark:border-neutral-800">
          {chapters.length
            ? <Progress items={chapters.flatMap(post => post.items)} label="SOP chapters progress" th="ความคืบหน้าบท SOP" />
            : <Progress items={posts.flatMap(post => post.items)} label="Overall progress" th="ความคืบหน้าโดยรวม" />}
          <p className="text-xs leading-relaxed text-neutral-500 dark:text-neutral-400">Based on pages opened and video sections watched{chapters.length ? ' in the SOP chapters' : ''}, with equal weight for each item. Activity does not confirm understanding.
            <Thai className="mt-1">{`คิดจากหน้าที่เปิดอ่านและช่วงวิดีโอที่ดูแล้ว${chapters.length ? 'ในบท SOP' : ''} โดยทุกรายการมีน้ำหนักเท่ากัน การเปิดดูไม่ได้ยืนยันว่าเข้าใจเนื้อหา`}</Thai></p>
        </div>
      </section>
      <BadgeShelf quizzes={quizzes} finalExam={finalExam} certificate={certificate} />
      <section className="space-y-4 rounded-2xl border border-neutral-200 dark:border-neutral-800 bg-white dark:bg-neutral-900 p-5">
        <h2 className="font-semibold">Table of contents</h2>
        <Contents finalExam={finalExam} posts={posts} onOpen={onOpen} quizzes={quizzes} exempt={exempt} onQuiz={onQuiz} />
      </section>
    </main>
  )
}
