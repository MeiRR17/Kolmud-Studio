# תוכנית מימוש 51: OpenMontageInc/OpenMontage

> **מאגר יעד:** `MeiRR17/Kolmud-Studio` | **עדיפות:** P2 | **החלטה:** REFERENCE | **מקור:** https://github.com/OpenMontageInc/OpenMontage

## נחיצות ופער מוצר
Assess integrated video studio only after HyperFrames proves insufficient.
מיקומו של הרכיב בפרויקט: existing local Hebrew-to-English voice studio, local MLX transcription, approved TTS and output SRT with model-weight controls. יש לבדוק את ה־HEAD, ענפי העבודה, ה־CI והקוד הקיים לפני שינוי כדי שלא תיווצר תשתית שנייה הפועלת במקביל.

## ארכיטקטורה, API וגבולות
Isolated licensed demo pipeline -> sample 10-second video with tracked assets.
ליצור adapter מאחורי feature flag כבוי, עם מזהה מקור מורשה, גרסת upstream/מודל, סטטוס, זמני ריצה, SHA-256 של קלט/פלט כשמתאים, וקישור ראיות. תוצרי ביניים יישמרו מחוץ ל־Git ולנתונים הקנוניים. אין ענן, שינוי מערכת, השבתה או חיבור הרשאות ללא אישור נפרד.

## תוכנית הגרסאות
**0.1 — אימות היתכנות:** זיהוי הפער המדויק והחלופה הקיימת, ADR החלטה, הגדרת צרכן/בעלים, ביצוע מיפוי תלויות.
**0.2 — אבטחה ורישוי:** לנעול commit/release ומודל, לבדוק LICENSE (תצפית: AGPL-3.0), תלויות, רישיונות מודלים/מידע, SBOM, תחומי רשת, זיכרון ו־CPU.
**0.3 — Vertical slice:** Compare operational overhead, editing control and quality vs HyperFrames. הרצה על נתונים סינתטיים/מורשים בלבד. לתעד baseline ללא הכלי והשוואה מדידה.
**0.4 — אינטגרציה:** קוד מתאם ואירועים, טיפול ב־timeouts ובשגיאות, גבולות משאבים, מתג הפעלה, בדיקות רכיב/תאימות והרשאות מינימליות.
**0.5 — איכות:** לאמת מקרי כשל, נתונים שגויים, מסמכים/טקסטים בעברית כשיש לכך קשר, שימור מקור, השפעת ביצועים והימנעות משינוי לרכיבי ליבה.
**1.0 — השקה מותנית:** רק אחרי PR, תיעוד, בעלות תפעולית, בדיקות בסביבה המתאימה, rollback והחלטת בעלים; לא כל P2 מצדיק שלב זה.

## תרחיש קבלה ומדדים
בפיילוט: Compare operational overhead, editing control and quality vs HyperFrames.
במבחן קבלה נדרשים נכונות פלט בהשוואת אמת, אפס גישה בלתי מורשית, שחזור תוצאה, מדידת latency/זיכרון/טוקנים אם רלוונטי, התנהגות offline וחזרה נקייה לנתיב הישן. אין להסתמך על ציונים שמופיעים בשיווק של upstream.

## בחירה, חלופות ובלמים
Avoid embedding whole platform into Learning_Modules.
**סיכון ממוקד:** AGPL-3.0 and dependencies; source videos/voices rights.
אם התועלת הנמדדת אינה מוכחת או שהרישוי אינו מתאים, יש לתעד defer/reject במקום להתקין. מצב `REFERENCE` הוא סטטוס תכנון, **לא הודעת הצלחה בהטמעה**.

## Checklist להשלמה
- [ ] רישיון קוד+מודל+מידע הוכח; גרסה מוצמדת
- [ ] סודיות/הרשאות/משאבים מאושרים ו־flag כבוי כברירת מחדל
- [ ] קוד או ניסוי השוואתי ותוצאות מדודות
- [ ] בדיקות יחידה, אינטגרציה, קצה ורגרסיה
- [ ] תיעוד הפעלה, ניטור, rollback וקישור ל־PR בעל ראיות
