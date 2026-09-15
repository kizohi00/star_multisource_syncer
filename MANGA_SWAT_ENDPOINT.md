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
| envelope `Lyc/a` | `count`, `next`, `prev`, `results` |
| بطاقة العمل | `seriesId`, `name`, `slug`, `poster`, `latestReleasedChapters` |
| الفصل المضمّن | `id`, `chapter`, `title`, `numberWithTitle` |

لا يضيف المصدر وقتاً مصطنعاً للفصل، لأن نموذج الفصل المضمّن في الـAPK لا
يحتوي على حقل timestamp. يتم حفظ رقم الفصل ومعرّفه والرابط العام، وتبقى
بيانات التفاصيل والتاريخ الكامل من مسارات الـparser الأصلية.

## فصل المسارات

- `https://appswat.com/v2/api/v1/series/releases/`: أحدث الفصول المعروضة في الرئيسية، وهو مسار
  polling في Star.
- `https://appswat.com/v2/api/v2/series/`: فهرس الأعمال وتفاصيل العمل
  المستخدمة عند الحاجة.
- `https://appswat.com/v2/api/v2/chapters/?serie=...`: التاريخ الكامل لفصول
  عمل محدد.
- `https://appswat.com/v2/api/v2/chapters/{id}/`: صور صفحات الفصل.

اختبار `tests/test_mangaswat.py` يثبت أن polling يطلب endpoint الأول مرة
واحدة ويحوّل الفصول المضمّنة دون تنفيذ طلب chapters إضافي لكل بطاقة.
