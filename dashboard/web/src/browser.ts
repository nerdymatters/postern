/** Messenger, Instagram, Facebook, Line, TikTok and friends open links in an
 *  embedded webview, and Google refuses to sign in there, so the landing page
 *  asks for a real browser instead of showing a button that cannot work. */
const EMBEDDED = /FBAN|FBAV|FB_IAB|FBIOS|Messenger|Instagram|Line\/|KAKAOTALK|NAVER|TikTok|musical_ly|BytedanceWebview|Snapchat|MicroMessenger|WeChat|Twitter|WhatsApp|GSA\//i

export function inAppBrowser(agent: string): boolean {
  return EMBEDDED.test(agent)
}
