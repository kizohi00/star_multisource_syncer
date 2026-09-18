# Manga Swat latest-chapters feed

هذا المستند يصف العقد الذي يستخدمه مصدر `mangaswat` في Star لاكتشاف
التحديثات.

## الطلب الموثّق

```http
GET https://appswat.com/v2/api/v1/series/releases/?page={page}&page_size=100
```

الـAPK يبني المسار بهذه الطبقات:

1. المضيف `https://appswat.com`.
2. جذر الـAPI `v2/api`.
3. مساحة المسارات `v1/`.
4. واصف المسار `series/releases/`.

ويضيف نظام paging في التطبيق معاملي `page` و`page_size`، مع بداية `page=1`
وحجم صفحة `100`.

## دليل التحليل الساكن

الاعتماد هنا على القيم الموجودة في APK المرفق:

| موضع الدليل | القيمة أو السلوك |
| --- | --- |
| route descriptor `Lvc/y` | `series/releases` |
| `Lvc/y.toString()` | `SeriesLatestReleases` |
| تدفق الشاشة الرئيسية `Lkf/a` | يستدعي `Lvc/y` |
| serializer الممرر للتدفق | `LatestReleaseSeriesCardSerializer` |
| envelope `Lyc/a` | `count`, `next`, `prev`/`previous`, `results` |
| بطاقة العمل في نموذج APK | `seriesId`, `name`, `slug`, `poster`, `latestReleasedChapters` |
| بطاقة العمل في الاستجابة الحالية | `serie_id`, `title`, `slug`, `poster`, `chapters`, `rating`, `views_count` |
| الفصل المضمّن في نموذج APK | `id`, `chapter`, `title`, `numberWithTitle` |
| الفصل في الاستجابة الحالية | `id`, `chapter`, `title`, `created_at`, `updated_at` |

لا يضيف المصدر وقتاً مصطنعاً للفصل. إذا أعاد endpoint الحالي
`created_at`/`updated_at` تُحفظ قيمة التاريخ الحقيقية، أما نموذج الفصل
المضمّن في الـAPK إذا خلا من timestamp فيبقى التاريخ فارغاً إلى أن يجلبه
enrichment من المسار الكامل.

## فصل المسارات

- `https://appswat.com/v2/api/v1/series/releases/`: أحدث الفصول المعروضة في الرئيسية، وهو مسار
  polling في Star.
- `https://appswat.com/v2/api/v2/series/`: فهرس الأعمال وتفاصيل العمل
  المستخدمة عند الحاجة.
- `https://appswat.com/v2/api/v2/chapters/?serie=...`: التاريخ الكامل لفصول
  عمل محدد.
- `https://appswat.com/v2/api/v2/chapters/{id}/`: صور صفحات الفصل.

## حالة العمل

يقرأ العامل حالة العمل من الحقل `status.name` في بطاقة/تفاصيل المصدر،
ويمررها إلى دالة التطبيع المشتركة. الـAPK المرفق للتحليل يحمل package
`com.swat.apps.manga`؛ وتظهر في موارده العربية `مستمر` (`on_going_status`)،
`مكتمل` (`completed_status`)، و`متوقف` (`hiatus_status`)، وفي موارده
الإنجليزية `on going` و`completed` و`hiatus`. يدعم parser صيغ المؤنث
والمرادفات التي تظهر في مصادر المشروع مثل `مستمرة` و`مكتملة` و`منتهية`
و`متوقفة` و`في استراحة`. كما تظهر حالة `coming`/«لم يتم نشره» في الـAPK،
لكنها ليست واحدة من حالات Star الثلاث ولا تُستخدم لتغيير الحالة. بعد ربط العمل بسلسلة
canonical، تتم مقارنة الحالة مع `series.story_status` وتحديثها عند الاختلاف.
لا تتم الكتابة إلى `series.translation_status` لأنه عمود مهجور في هذا
التصميم.

توحيد الحالات لجميع المصادر ومسارات الإنشاء موثق في
[`docs/STATUS_NORMALIZATION.md`](STATUS_NORMALIZATION.md).

اختبارات `tests/test_mangaswat.py` تثبت أن polling يطلب endpoint الأول مرة
واحدة ويحوّل الفصول المضمّنة دون تنفيذ طلب chapters إضافي لكل بطاقة.
