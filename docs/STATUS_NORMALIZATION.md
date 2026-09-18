# توحيد حالات الأعمال

## النتيجة القياسية

يمر كل مصدر عبر
`mangastar_multisource.domain.status.story_status_from_payload` قبل الكتابة.
الدالة لا تعيد إلا:

| القيمة في Manga Star | أمثلة القيم المقبولة |
| --- | --- |
| `ongoing` | `ongoing`, `on going`, `in progress`, `مستمر`, `مستمرة`, `مستمره`, `جاري`, `جارية` |
| `completed` | `completed`, `complete`, `finished`, `مكتمل`, `مكتملة`, `مكتمله`, `منتهي`, `منتهية`, `منتهيه` |
| `hiatus` | `hiatus`, `paused`, `on hold`, `متوقف`, `متوقفة`, `متوقفه`, `في استراحة`, `استراحة` |

القيمة غير المعروفة لا تُخَمَّن ولا تُكتب. كذلك `coming` و«لم يتم نشره» لا
تتحول إلى واحدة من الحالات الثلاث، لذلك لا تغيّر الحالة الحالية.

## دليل المصدر وطريقة الاستخراج

| المصدر | دليل/حقل الحالة المستخدم في الكود | نقطة التطبيع |
| --- | --- | --- |
| Manga Swat | كائن API في `status`، واسمه عادةً `status.name` | `_parse_series` يمرره إلى الدالة المشتركة |
| AzoraFly | `status` إن وُجد في بطاقة API، أو صفوف HTML المسمّاة `Status`/`State`/`الحالة` | بطاقة latest وتفاصيل العمل |
| 3Asq | تخطيط Madara المشترك، عبر صفوف metadata المسمّاة | `MadaraLatestAdapter` |
| SparkManga | نفس تخطيط Madara؛ التفاصيل تمر عبر `WordPressAjaxMixin` | parser المشترك للتغذية والتفاصيل |
| MangaTek | صفوف HTML المسمّاة `Status`/`State`/`الحالة` أو attributes صريحة | بطاقة latest وتفاصيل العمل |
| Team-X | الحقل القديم `state` المستخرج من النص `الحالة:` | يحفظ `state` للتوافق ويضع `status` الموحّد |

إذا لم تظهر الحالة في بطاقة latest، تبقى `status` فارغة إلى أن يجلب enrichment
تفاصيل العمل. هذا مقصود حتى لا نضع حالة مبنية على العنوان أو الوصف.

## ما يثبته الـAPK المرفق

الـAPK المتاح للتحليل يحمل package `com.swat.apps.manga`، أي أنه تطبيق Manga
Swat وليس APK لتطبيق Manga Peak متعدد المصادر. الأدلة الساكنة فيه تثبت عقد
Manga Swat فقط:

- النموذج يستخدم `status`/`getStatus()` وكائن الحالة يحتوي الاسم.
- مورد العربية يعرّف `on_going_status` = «مستمر»، و`completed_status` =
  «مكتمل»، و`hiatus_status` = «متوقف».
- مورد الإنجليزية يعرّف المقابلات `on going` و`completed` و`hiatus`.
- يوجد أيضاً `coming`/«لم يتم نشره»، وهي حالة عرض لا تدخل في الحالات الثلاث
  المطلوبة في قاعدة Manga Star.

لذلك لا يدّعي هذا المستند أن الـAPK أثبت صيغة status لكل موقع ويب آخر؛ صيغ
المصادر الأخرى أعلاه موثقة من parsers الموجودة في المشروع نفسه، والقيم غير
المعروفة تُتجاهل بأمان.

## مسارا التحديث

1. عند استقبال latest feed، يحدّث `_upsert_work_cursor` الحالة للسلسلة المرتبطة
   إذا وُجدت حالة قياسية واختلفت عن القيمة الحالية.
2. عند إنشاء سلسلة جديدة تلقائياً أو عبر ترقية معتمدة، يستخرج
   `_create_canonical_series_cursor` الحالة من `payload_json` ويضعها أثناء
   `INSERT INTO series`.

المساران يكتبان `series.story_status` فقط. لا يغير syncer
`series.translation_status`.
