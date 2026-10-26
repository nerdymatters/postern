// Postern UI icons: 24 px grid, 2 px stroke, round ends, drawn in currentColor.
// Size them with a class (size-4 = 16 px next to 14 px text, size-5 = 20 px in buttons)
// and colour them through the text colour of the icon or its parent.
// They are decorative (aria-hidden); give icon-only buttons an aria-label.
import type { SVGProps } from 'react'

type IconProps = SVGProps<SVGSVGElement>

function Icon({ children, ...props }: IconProps) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      width={20}
      height={20}
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    >
      {children}
    </svg>
  )
}

/** Admin */
export const IconAdmin = (props: IconProps) => (
  <Icon {...props}><circle cx="7.5" cy="12" r="5" /><path d="M12.5 12h9v3.5M18 12v3" /></Icon>
)

/** Update selfie */
export const IconUpdateSelfie = (props: IconProps) => (
  <Icon {...props}><path d="M5 21V10a7 7 0 0 1 14 0v11z" /><circle cx="12" cy="10.5" r="3" /><path d="M7.5 21a4.5 4.5 0 0 1 9 0" /></Icon>
)

/** Sign out */
export const IconSignOut = (props: IconProps) => (
  <Icon {...props}><path d="M12 4h-1a6 6 0 0 0-6 6v11h7" /><path d="M10 12.5h11M17.5 9l3.5 3.5-3.5 3.5" /></Icon>
)

/** PDF */
export const IconPdf = (props: IconProps) => (
  <Icon {...props}><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" /><path d="M14 3v5h5M9 13h6M9 17h4" /></Icon>
)

/** Video */
export const IconVideo = (props: IconProps) => (
  <Icon {...props}><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" /><path d="M14 3v5h5" /><path d="M10 11.5v6l5-3z" /></Icon>
)

/** Open camera */
export const IconCamera = (props: IconProps) => (
  <Icon {...props}><path d="M9 7V6a3 3 0 0 1 6 0v1" /><rect x="3" y="7" width="18" height="13" rx="3" /><circle cx="12" cy="13.5" r="3.5" /></Icon>
)

/** Use this photo */
export const IconCheck = (props: IconProps) => (
  <Icon {...props}><path d="M5 12.5l4.5 4.5L19 7.5" /></Icon>
)

/** Retake */
export const IconRetake = (props: IconProps) => (
  <Icon {...props}><path d="M4.5 12a7.5 7.5 0 1 0 2.2-5.3L4.5 9" /><path d="M4.5 4.5V9H9" /></Icon>
)

/** Continue */
export const IconArrowRight = (props: IconProps) => (
  <Icon {...props}><path d="M4 12h15M13.5 6.5 19 12l-5.5 5.5" /></Icon>
)

/** Create draft */
export const IconNewPost = (props: IconProps) => (
  <Icon {...props}><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" /><path d="M14 3v5h5M12 11.5v6M9 14.5h6" /></Icon>
)

/** Rename */
export const IconRename = (props: IconProps) => (
  <Icon {...props}><path d="M4 20l1-4.5L15.5 5a2.47 2.47 0 0 1 3.5 3.5L8.5 19z" /><path d="M13.5 7l3.5 3.5" /></Icon>
)

/** Manage */
export const IconManage = (props: IconProps) => (
  <Icon {...props}><path d="M4 8h9M17 8h3M4 16h3M11 16h9" /><circle cx="15" cy="8" r="2" /><circle cx="9" cy="16" r="2" /></Icon>
)

/** Report */
export const IconReport = (props: IconProps) => (
  <Icon {...props}><path d="M6 20v-6M12 20V5M18 20v-9" /></Icon>
)

/** Staff progress */
export const IconStaff = (props: IconProps) => (
  <Icon {...props}><circle cx="9" cy="8" r="3.5" /><path d="M3 20.5a6 6 0 0 1 12 0" /><path d="M15.5 4.6a3.5 3.5 0 0 1 0 6.8M18.2 14.6a6 6 0 0 1 3.3 5.9" /></Icon>
)

/** Enrollment photos */
export const IconEnrollmentPhotos = (props: IconProps) => (
  <Icon {...props}><path d="M3 21v-9a6 6 0 0 1 12 0v9z" /><circle cx="9" cy="13" r="2.3" /><path d="M5.8 21a3.2 3.2 0 0 1 6.4 0" /><path d="M10.5 4.03A6 6 0 0 1 21 8v9h-4" /></Icon>
)

/** Not on the list */
export const IconNotOnList = (props: IconProps) => (
  <Icon {...props}><circle cx="9" cy="8" r="3.5" /><path d="M3 20.5a6 6 0 0 1 12 0" /><path d="M17 9.5l4 4M21 9.5l-4 4" /></Icon>
)

/** Find a post */
export const IconSearch = (props: IconProps) => (
  <Icon {...props}><circle cx="10.5" cy="10.5" r="6.5" /><path d="M15.5 15.5l5 5" /></Icon>
)

/** Archived */
export const IconArchive = (props: IconProps) => (
  <Icon {...props}><rect x="3" y="4" width="18" height="5" rx="1.5" /><path d="M5 9v9a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9M10 13h4" /></Icon>
)

/** Chapter quiz */
export const IconQuiz = (props: IconProps) => (
  <Icon {...props}><path d="M9 4.5H7a2 2 0 0 0-2 2V19a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V6.5a2 2 0 0 0-2-2h-2" /><rect x="9" y="3" width="6" height="3" rx="1" /><path d="M9 13.5l2 2 4-4" /></Icon>
)

/** Privacy; also a locked chapter */
export const IconPrivacy = (props: IconProps) => (
  <Icon {...props}><rect x="5" y="10.5" width="14" height="10.5" rx="2.5" /><path d="M8 10.5V8a4 4 0 0 1 8 0v2.5M12 14.5V17" /></Icon>
)

/** Badges; the certificate */
export const IconAward = (props: IconProps) => (
  <Icon {...props}><circle cx="12" cy="9" r="6" /><path d="M8.5 13.9 7 21.5l5-2.8 5 2.8-1.5-7.6" /></Icon>
)

/** Print or save as PDF */
export const IconPrint = (props: IconProps) => (
  <Icon {...props}><path d="M7 9V3.5h10V9M7 17.5H5a2 2 0 0 1-2-2V11a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v4.5a2 2 0 0 1-2 2h-2" /><rect x="7" y="14" width="10" height="7" rx="1" /></Icon>
)
