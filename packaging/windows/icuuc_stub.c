// SPDX-FileCopyrightText: 2026 Gus Salazar
// SPDX-License-Identifier: GPL-3.0-or-later
/* TEST-ONLY stub of Windows' system icuuc.dll for running Qt 6 under Wine 9.0 (which lacks it).
   Never shipped. Every converter open fails (U_FILE_ACCESS_ERROR), so Qt falls back to its
   built-in UTF-8/UTF-16/Latin-1 codecs; zero available converters are reported. */
#include <stddef.h>
#include <stdint.h>
typedef int UErrorCode; typedef uint16_t UChar; typedef void UConverter;
#define EXPORT __declspec(dllexport)
static void set_err(UErrorCode *e){ if(e && *e<=0) *e = 4; /* U_FILE_ACCESS_ERROR */ }
EXPORT UConverter* ucnv_open(const char *n, UErrorCode *e){ (void)n; set_err(e); return NULL; }
EXPORT void ucnv_close(UConverter *c){ (void)c; }
EXPORT void UCNV_FROM_U_CALLBACK_SUBSTITUTE(void){}
EXPORT void UCNV_TO_U_CALLBACK_SUBSTITUTE(void){}
EXPORT void ucnv_reset(UConverter *c){ (void)c; }
EXPORT int8_t ucnv_getMaxCharSize(const UConverter *c){ (void)c; return 4; }
EXPORT const char* ucnv_getName(const UConverter *c, UErrorCode *e){ (void)c; set_err(e); return ""; }
EXPORT void ucnv_getToUCallBack(const UConverter *c, void **a, const void **b){ (void)c; if(a)*a=NULL; if(b)*b=NULL; }
EXPORT void ucnv_getFromUCallBack(const UConverter *c, void **a, const void **b){ (void)c; if(a)*a=NULL; if(b)*b=NULL; }
EXPORT void ucnv_setToUCallBack(UConverter *c, void *n, const void *x, void **o, const void **ox, UErrorCode *e){ (void)c;(void)n;(void)x;(void)o;(void)ox; set_err(e); }
EXPORT void ucnv_setFromUCallBack(UConverter *c, void *n, const void *x, void **o, const void **ox, UErrorCode *e){ (void)c;(void)n;(void)x;(void)o;(void)ox; set_err(e); }
EXPORT void ucnv_cbToUWriteUChars(void *a, const UChar *s, int32_t l, int32_t o, UErrorCode *e){ (void)a;(void)s;(void)l;(void)o; set_err(e); }
EXPORT void ucnv_cbFromUWriteUChars(void *a, const UChar **s, const UChar *l, int32_t o, UErrorCode *e){ (void)a;(void)s;(void)l;(void)o; set_err(e); }
EXPORT void ucnv_toUnicode(UConverter *c, UChar **t, const UChar *tl, const char **s, const char *sl, int32_t *o, int8_t f, UErrorCode *e){ (void)c;(void)t;(void)tl;(void)s;(void)sl;(void)o;(void)f; set_err(e); }
EXPORT void ucnv_fromUnicode(UConverter *c, char **t, const char *tl, const UChar **s, const UChar *sl, int32_t *o, int8_t f, UErrorCode *e){ (void)c;(void)t;(void)tl;(void)s;(void)sl;(void)o;(void)f; set_err(e); }
EXPORT int32_t ucnv_toUCountPending(const UConverter *c, UErrorCode *e){ (void)c; set_err(e); return 0; }
EXPORT int32_t ucnv_fromUCountPending(const UConverter *c, UErrorCode *e){ (void)c; set_err(e); return 0; }
EXPORT const char* ucnv_getStandardName(const char *n, const char *s, UErrorCode *e){ (void)n;(void)s; set_err(e); return NULL; }
EXPORT const char* ucnv_getAvailableName(int32_t i){ (void)i; return NULL; }
EXPORT int32_t ucnv_countAvailable(void){ return 0; }
