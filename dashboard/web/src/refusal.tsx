import type { Refusal, RefusalCode } from './api'
import { Bilingual, Button, Centred, Label, Panel } from './ui'
import { IconSignOut } from './PosternIcons'

const STATES: Record<Exclude<RefusalCode, 'consent_required' | 'sign_in_required' | 'selfie_required'>, { title: string; hint: string }> = {
  access_denied: {
    title: 'Your access is no longer available / คุณไม่มีสิทธิ์เข้าถึงแล้ว',
    hint: 'If you think this is wrong, ask your manager to check the staff list. / หากคุณคิดว่าเกิดข้อผิดพลาด กรุณาให้ผู้จัดการตรวจสอบรายชื่อพนักงาน',
  },
  authorization_source_unavailable: {
    title: 'Access temporarily paused while we verify permissions / หยุดการเข้าถึงชั่วคราวระหว่างตรวจสอบสิทธิ์',
    hint: 'We cannot check the staff list right now. Please try again in a minute. / ขณะนี้ไม่สามารถตรวจสอบรายชื่อพนักงานได้ กรุณาลองใหม่ในอีกสักครู่',
  },
  selfie_unavailable: {
    title: 'Your enrollment photo is temporarily unavailable / รูปยืนยันตัวตนของคุณไม่พร้อมใช้งานชั่วคราว',
    hint: 'Please try again in a moment. Your reading progress is saved. / กรุณาลองใหม่ในอีกสักครู่ ความคืบหน้าในการอ่านของคุณถูกบันทึกไว้แล้ว',
  },
  audit_unavailable: {
    title: 'Content is paused because its audit record could not be saved / หยุดแสดงเนื้อหาชั่วคราวเนื่องจากบันทึกการใช้งานไม่ได้',
    hint: 'Postern never shows a page it cannot record. Try again in a minute. / ระบบ Postern จะไม่แสดงหน้าที่ไม่สามารถบันทึกการใช้งานได้ กรุณาลองใหม่ในอีกสักครู่',
  },
}

export function Refused({ refusal, onRetry, onSignOut }: { refusal: Refusal; onRetry: () => void; onSignOut: () => void }) {
  const state = STATES[refusal.code as keyof typeof STATES]
  return (
    <Centred title={<Bilingual text={state.title} />}>
      <Panel className="space-y-3">
        <p className="text-neutral-800 dark:text-neutral-200"><Bilingual text={refusal.message} /></p>
        <p className="text-sm text-neutral-500 dark:text-neutral-400"><Bilingual text={state.hint} /></p>
      </Panel>
      <div className="flex gap-3">
        <Button onClick={onRetry} className="flex-1"><Label en="Try again" th="ลองอีกครั้ง" /></Button>
        <Button variant="secondary" onClick={onSignOut} className="flex-1"><IconSignOut className="size-5" /><Label en="Sign out" th="ออกจากระบบ" /></Button>
      </div>
    </Centred>
  )
}
