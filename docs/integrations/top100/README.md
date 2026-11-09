# מפת תוכניות האינטגרציה ב־MeiRR17/Kolmud-Studio

מיפוי הבעלות עבר סקירה שנייה לפי תוכן המאגר, לא רק לפי שם הכלי. **2 תוכניות עצמאיות** משויכות למאגר הזה. הקשר: Original Torah and lecture audio/video studio with local models and explicit media licensing.

| # | מאגר המקור ותוכנית מימוש | עדיפות | החלטה | צורך |
|---:|---|---|---|---|
| 41 | [heygen-com/hyperframes](./041-heygen-com-hyperframes.md) | P0 | GATED_PILOT | Generate professionally consistent original branded video intros and captions. |
| 51 | [OpenMontageInc/OpenMontage](./051-openmontageinc-openmontage.md) | P2 | REFERENCE | Assess integrated video studio only after HyperFrames proves insufficient. |

## הוראות יישום

1. לוודא חסר יכולת נוכחית לפי קוד אמיתי והענפים הפעילים; לא להסתמך רק על כוכבים או תיאור upstream.
2. לבדוק commit/release, LICENSE, זכויות מודלים ונתונים, שרשרת תלויות, עלות והרשאות. אין פטור מרישוי כשהקוד קיים ב־GitHub.
3. לבצע פיילוט עם fixture מורשה, baseline והשוואה מדידה. אם כלי אחר כבר פועל היטב, לשקול defer.
4. להטמיע קוד רק behind opt-in flag, בלי כתיבה אוטומטית למקורות/פרסום/שליחת סודות, ועם אפשרות rollback.
5. להעלות קוד + בדיקות ותוצאות מדודות ל־Draft PR ולהפעיל רק לאחר אימות בעלים.

**חשוב:** תוכניות קיימות ≠ ספריות מותקנות ≠ בדיקות שעברו ≠ פריסה עובדת. כלי במצב REFERENCE/COMPARE/GATED לא נחשב כחיבור פעיל.

קובצי המדדים ותוצאות הבדיקות של כל כלי צריכים להיכנס ל־PR של אותו מוצר; אין להעלות תוצרי קבצים פרטיים, תמלולים או קרדנציאלים לגיטהאב.
