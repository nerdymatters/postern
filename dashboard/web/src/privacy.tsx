import { Link } from './routes'
import { Centred, Panel } from './ui'
import { useBranding } from './branding'

/** Public, and deliberately independent of the API: it has to be readable by someone who cannot sign in,
 *  and while the office server is down. It says what the monitoring notice says, at more length. */
export function Privacy() {
  const { organization_name: organization, privacy_contact_email: contact } = useBranding()
  return (
    <Centred title="Privacy">
      <Panel className="space-y-4 text-sm leading-relaxed text-neutral-800 dark:text-neutral-200">
        <p>
          Postern is a private training site that {organization || 'your organization'} runs for its own staff. It holds confidential
          company material, so it keeps a record of who opened what.
        </p>
        <Section title="What is recorded">
          Your sign-ins, the documents and pages served to you, video sections played, the time, your browser and device type, and your
          IP address. Pages and the video player display your email address and the time, so a screenshot or a photo
          of the screen still shows whose session it came from.
        </Section>
        <Section title="Who can see it">
          Company administrators. Opening a document is recorded when its content is sent to you; that does not
          prove it was read. Video progress counts sections played in the browser, not proof of attention. The video watermark is a player overlay; it is not baked into the video file.
        </Section>
        <Section title="Enrollment selfies">
          Where selfie enrollment is enabled, Postern asks for a photo every 30 days or in a new browser. New and replacement photos are checked on Postern’s server for one clear face before saving. No face templates or detection landmarks are retained, and no external face-analysis service receives your photo. This does not verify identity or liveness. A small copy appears in your personal watermark and is available to company administrators with activity records. Replaced or expired photos are deleted after the retention period set by the organization (90 days by default); non-image activity records remain. The camera is used only when you choose to capture a photo. Postern does not perform face matching or identity verification.
        </Section>
        <Section title="Your Google account">
          You sign in with Google so the company can check your address against its staff list. Postern receives
          only your name and email address. It cannot see your Gmail, Drive, contacts or calendar, and it never
          asks for them. Its use of information received from Google APIs adheres to the Google API Services
          User Data Policy, including the Limited Use requirements.
        </Section>
        <Section title="Where it is kept">
          On a computer in the company's office and in the company's own Google Drive. It is not sold, not used
          for advertising, and not shared outside the company. {organization || 'Your organization'} decides how long these records are
          kept.
        </Section>
        <Section title="Questions">
          {contact ? <>Ask your manager, or write to{' '}<a className="underline" href={`mailto:${encodeURIComponent(contact)}`}>{contact}</a>.</> : 'Ask your manager or the administrator of this Postern installation.'}
        </Section>
        <p className="pt-2 text-center"><Link to="/" className="underline">Back to Postern</Link></p>
        <p className="text-center"><a href="/third-party-licenses.txt" className="underline">Licenses and notices</a></p>
      </Panel>
    </Centred>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-1">
      <h2 className="font-semibold text-neutral-950 dark:text-neutral-50">{title}</h2>
      <p>{children}</p>
    </section>
  )
}
