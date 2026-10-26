# Postern logo kit

The logo is **04 poster·n**: "poster" followed by an n whose opening is a door.

## Files

```
wordmark/
  postern-wordmark.svg         Ink letters, emerald door. Use on white or on #F4F4F2
  postern-wordmark-white.svg   White letters, emerald-500 door. Use on dark backgrounds
  postern-wordmark-ink.svg     One colour, no door. Use where only one colour is possible
icon/
  postern-icon.svg             Rounded app icon, for docs, chat and slides
  postern-icon-1024.png
icons/
  *.svg                        The same icons as separate files (24 px, currentColor)
favicon/                       Deployed copies live in dashboard/web/public
  favicon.ico                  16 px plain arch, plus 32 px and 48 px with the door
  apple-touch-icon.png         180 px, full bleed (iOS rounds the corners itself)
  icon-192.png, icon-512.png   Full bleed; the n sits inside Android's circular safe zone
  site.webmanifest
  favicon-16.svg, favicon-32.svg, favicon-16/32/48.png   Sources for the hand-fitted small sizes
```

## In the app

- Header and landing page: `Wordmark` in `dashboard/web/src/ui.tsx` renders `dashboard/web/src/PosternWordmark.tsx` at `h-6` (24 px). The letters are outlines, so no font is loaded; `role="img"` and `aria-label="Postern"` name the header link.
- UI icons: `dashboard/web/src/PosternIcons.tsx` contains 22 icons. The SVG files in `icons/` are the same drawings. The sun/moon theme icons live separately in `dashboard/web/src/theme.tsx`; their licenses are in `dashboard/web/public/third-party-licenses.txt`, which also notes that `IconAward` (`icons/award.svg`) follows the shape of Lucide's award icon.
- Head tags are in `dashboard/web/index.html`. The favicon, touch icon, Android icons and manifest are in `dashboard/web/public`, which Vite copies to the site root.

There is no SVG favicon, on purpose. A browser scales one SVG to every size, and that would put the shrunken door back into the 16 px tab icon. The .ico contains a separate 16 px drawing instead.

The manifest sets `"display": "browser"` so that Google sign-in keeps working. Change it to `"standalone"` only after you've tested sign-in from the home-screen icon.

## Icons

The UI icons are line drawings on a 24 px grid with a 2 px stroke and round ends. The arch from the logo comes back in the sign-out door, the selfie frame, the camera and the lock. They draw in `currentColor`, so they take the colour of the text around them and never add green of their own. They're decorative (`aria-hidden`), so the text or an `aria-label` names the action.

| Where | Action | Component |
|---|---|---|
| Header | Day/night mode | `ThemeToggle` (sun/moon) |
| Header | Admin | `IconAdmin` (key) |
| Header | Update selfie | `IconUpdateSelfie` |
| Header, selfie page | Sign out | `IconSignOut` |
| Reading list | PDF, Video | `IconPdf`, `IconVideo` |
| Selfie page | Open camera, Take photo, Use this photo, Retake | `IconCamera`, `IconCamera`, `IconCheck`, `IconRetake` |
| Selfie page | Continue to admin without a selfie | `IconAdmin` |
| Links that end in "→" | Staff progress →, Tried to sign in… → | `IconArrowRight` in place of the → character |
| Admin | Create draft, Rename, Manage, Report | `IconNewPost`, `IconRename`, `IconManage`, `IconReport` |
| Admin | Staff progress, Enrollment photos, Not on the list | `IconStaff`, `IconEnrollmentPhotos`, `IconNotOnList` |
| Admin | Find a post, Archived | `IconSearch`, `IconArchive` |
| Sign-in page | Privacy | `IconPrivacy` |
| Reading list, quiz | Chapter quiz; locked chapter | `IconQuiz`; `IconPrivacy` (the lock) |
| Home, badges, quiz result, Staff progress | View certificate; the certificate is ready | `IconAward` |
| Certificate page, Staff progress | Print or save as PDF; Print certificate | `IconPrint` |

Use 16 px (`size-4`) beside 14 px text and 20 px (`size-5`) inside 16 px buttons, with a 6–8 px gap.

The authenticated header has up to four controls: theme, Admin (admins only), Update selfie (when enabled), and Sign out. They share the rounded, outlined `header-action` style in `dashboard/web/src/index.css`. Below the `sm` breakpoint (640 px), show icons in 44 × 44 px tap targets and keep action labels for screen readers. On wider screens, controls are 36 px tall and show the action labels; theme stays icon-only with an accessible name.

The header in `dashboard/web/src/app.tsx` follows this pattern:

```tsx
import { IconAdmin, IconUpdateSelfie, IconSignOut } from "./PosternIcons";
import { ThemeToggle } from "./theme";

const item = "header-action";
const label = "sr-only sm:not-sr-only";

// dark and toggle come from useTheme(); role/config checks live in App.
<nav className="flex items-center gap-1 text-sm sm:gap-2">
  <ThemeToggle dark={dark} onToggle={toggle} />
  <a href="/admin" className={item}>
    <IconAdmin className="size-5 sm:size-4" /><span className={label}>Admin</span>
  </a>
  <button type="button" className={item}>
    <IconUpdateSelfie className="size-5 sm:size-4" /><span className={label}>Update selfie</span>
  </button>
  <button type="button" className={item}>
    <IconSignOut className="size-5 sm:size-4" /><span className={label}>Sign out</span>
  </button>
</nav>
```

Keep the Google button on the sign-in page as Google supplies it. Its own "G" mark has to stay.

## Colours

| Name        | Hex       | Tailwind    | Used for |
|-------------|-----------|-------------|----------|
| Ink         | `#171717` | neutral-900 | Letters |
| Emerald     | `#007A55` | emerald-700 | The door. It's the same green as the progress bars |
| Deep green  | `#004F3B` | emerald-900 | The door in the app icon (9.6:1 against the white arch) |
| Emerald 500 | `#00BC7D` | emerald-500 | The door on dark backgrounds |

## Rules

- Keep the wordmark at least 16 px tall on screen. Below that, use the icon.
- Leave clear space around the wordmark equal to the height of the "o".
- The door is the only green in the header. Don't put other green elements next to it, and don't recolour the door to show a status.
- Elsewhere in the interface, use green for progress bars and Published status badges. Post status badges use amber for Draft, blue for Publishing, green for Published, and grey for Archived in both themes; staff-progress links stay neutral.
- The wordmark is lowercase on purpose. The arch-shaped n is what makes this logo, and a capital N has no arch.
- A half-open door was tested. It reads at 64 px and up, but at 32 px it looks like a half-filled n, so the door stays closed.
