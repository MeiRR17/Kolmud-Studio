# תוכנית מימוש 41: heygen-com/hyperframes

> **מאגר יעד:** `MeiRR17/Kolmud-Studio` | **עדיפות:** P0 | **החלטה:** GATED_PILOT | **מקור:** https://github.com/heygen-com/hyperframes

## נחיצות ופער מוצר
Generate professionally consistent original branded video intros and captions.
מיקומו של הרכיב בפרויקט: existing local Hebrew-to-English voice studio, local MLX transcription, approved TTS and output SRT with model-weight controls. יש לבדוק את ה־HEAD, ענפי העבודה, ה־CI והקוד הקיים לפני שינוי כדי שלא תיווצר תשתית שנייה הפועלת במקביל.

## ארכיטקטורה, API וגבולות
Original HTML/CSS + rights-cleared media -> local MP4 and source render manifest.
ליצור adapter מאחורי feature flag כבוי, עם מזהה מקור מורשה, גרסת upstream/מודל, סטטוס, זמני ריצה, SHA-256 של קלט/פלט כשמתאים, וקישור ראיות. תוצרי ביניים יישמרו מחוץ ל־Git ולנתונים הקנוניים. אין ענן, שינוי מערכת, השבתה או חיבור הרשאות ללא אישור נפרד.

## תוכנית הגרסאות
**0.1 — אימות היתכנות:** זיהוי הפער המדויק והחלופה הקיימת, ADR החלטה, הגדרת צרכן/בעלים, ביצוע מיפוי תלויות.
**0.2 — אבטחה ורישוי:** לנעול commit/release ומודל, לבדוק LICENSE (תצפית: Apache-2.0), תלויות, רישיונות מודלים/מידע, SBOM, תחומי רשת, זיכרון ו־CPU.
**0.3 — Vertical slice:** Render 10-second Hebrew RTL 1080p title slide; verify text legibility/audio ownership. הרצה על נתונים סינתטיים/מורשים בלבד. לתעד baseline ללא הכלי והשוואה מדידה.
**0.4 — אינטגרציה:** קוד מתאם ואירועים, טיפול ב־timeouts ובשגיאות, גבולות משאבים, מתג הפעלה, בדיקות רכיב/תאימות והרשאות מינימליות.
**0.5 — איכות:** לאמת מקרי כשל, נתונים שגויים, מסמכים/טקסטים בעברית כשיש לכך קשר, שימור מקור, השפעת ביצועים והימנעות משינוי לרכיבי ליבה.
**1.0 — השקה מותנית:** רק אחרי PR, תיעוד, בעלות תפעולית, בדיקות בסביבה המתאימה, rollback והחלטת בעלים; לא כל P2 מצדיק שלב זה.

## תרחיש קבלה ומדדים
בפיילוט: Render 10-second Hebrew RTL 1080p title slide; verify text legibility/audio ownership.
במבחן קבלה נדרשים נכונות פלט בהשוואת אמת, אפס גישה בלתי מורשית, שחזור תוצאה, מדידת latency/זיכרון/טוקנים אם רלוונטי, התנהגות offline וחזרה נקייה לנתיב הישן. אין להסתמך על ציונים שמופיעים בשיווק של upstream.

## בחירה, חלופות ובלמים
Use as small title/overlay renderer, not replace a complete NLE or audio engine.
**סיכון ממוקד:** Fonts, stock, third-party music and face assets need rights.
אם התועלת הנמדדת אינה מוכחת או שהרישוי אינו מתאים, יש לתעד defer/reject במקום להתקין. מצב `GATED_PILOT` הוא סטטוס תכנון, **לא הודעת הצלחה בהטמעה**.

## Checklist להשלמה
- [ ] רישיון קוד+מודל+מידע הוכח; גרסה מוצמדת
- [ ] סודיות/הרשאות/משאבים מאושרים ו־flag כבוי כברירת מחדל
- [ ] קוד או ניסוי השוואתי ותוצאות מדודות
- [ ] בדיקות יחידה, אינטגרציה, קצה ורגרסיה
- [ ] תיעוד הפעלה, ניטור, rollback וקישור ל־PR בעל ראיות

## מימוש ראשוני שהועלה בענף זה

קיים כעת `scripts/hyperframes_local_render.py` — מתאם אופציונלי שמפעיל **רק בינארי HyperFrames שכבר הותקן**, על פרויקט מקומי ומורשה *מחוץ* ל־Git. ברירת המחדל היא plan בלבד; `--run` מפעיל `hyperframes render`. הקוד אינו מריץ `npx`, אינו מתקין תלויות ואינו מפרסם סרטונים.

```sh
python3 scripts/hyperframes_local_render.py --project /path/to/approved-original-video
python3 scripts/hyperframes_local_render.py --project /path/to/approved-original-video --run
python3 -m unittest discover -s tests -p test_hyperframes_local_render.py -v
```

בדיקות יחידה משתמשות ב־runner מדומה. לא בוצע רינדור MP4 אמיתי או אימות זכויות של הנכסים. לפני הפעלה ב־Mac יש לוודא רישיון תוכנה ותוכן, מקור הפרויקט, גרסת Node 22+, ffmpeg ויצוא MP4 מדויק. יש להפעיל חסימת רשת ברמת OS אם לא רוצים אפשרות גישה לרשת — סינון environment בלבד אינו מהווה sandbox.
