# Manga Star Multi-Source Syncer

هذا المشروع يجلب الأعمال والفصول من المصادر المدعومة، يحفظ ملاحظات المزامنة، ثم ينشر الفصول ذات الصفحات المتحققة في قاعدة Manga Star.

## ما تم إصلاحه

- عند إنشاء عمل جديد، تُستخدم قائمة الفصول الكاملة من المصدر قبل بدء النشر. في AzoraFly لا تكفي صفحة العمل وحدها؛ فالصفحة تعرض الفصل الأول وآخر 20 فصلًا فقط، لذلك يستخدم الـ syncer endpoint الموقع العام الذي يعيد كل الفصول.
- تُنظَّف أوصاف AzoraFly من وسوم HTML قبل تخزينها.
- يُستخرج وصف القصة الحقيقي من 3Asq وSparkManga، بدل حاوية البيانات التي تحتوي التقييم والتصنيفات وباقي معلومات العمل.
- حد `MANGA_CHAPTER_PROMOTION_LIMIT` يخص الفصول الجديدة للأعمال الموجودة فقط. إنشاء عمل جديد يحاول نشر كل فصوله المرصودة، وتبقى الفصول المقفلة أو الفاشلة لإعادة المحاولة لاحقًا.

## قاعدتا البيانات

يستخدم البرنامج اتصالًا واحدًا إلى نفس خادم MariaDB، لكنّه يقرأ ويكتب:

- `MANGA_DB_*`: قاعدة المحتوى، وتحتوي جداول `series` و`chapters` و`pages`.
- `MANGA_SYNC_DB_NAME`: قاعدة بيانات المزامنة، وتحتوي الجداول التي تبدأ بـ `ms_` فقط. يستخدم الاتصال نفس الخادم وبيانات اعتماد `MANGA_DB_*`.

يجب إنشاء قاعدة المزامنة ومنح المستخدم صلاحية الوصول إليها قبل تشغيل Railway. يجب أن تكون القاعدتان على نفس خادم MariaDB، لأن البرنامج يستخدم معاملات مشتركة بين المخططين.

مثال الاختبار:

```text
MANGA_DB_NAME=zhffrycs_star_copy
MANGA_SYNC_DB_NAME=zhffrycs_star_sync_copy
```

في بيئة الإنتاج استخدم اسم قاعدة الإنتاج في `MANGA_DB_NAME` واسمًا منفصلًا في `MANGA_SYNC_DB_NAME`، ثم أضف `MANGA_ALLOW_NON_COPY_DB=1` بعد التأكد من القيم. لا تضع كلمات المرور أو التوكنات داخل GitHub.

أمر migration ينشئ جداول `ms_*` في قاعدة المزامنة، بينما تبقى الجداول الأساسية غير المؤهلة في قاعدة المحتوى. أمر `inspect` يعرض اسم القاعدتين وعدّادات جداول المزامنة.

## تشغيل Railway

يستمر إعداد Railway الحالي كما هو:

- `preDeployCommand` لتطبيق migrations.
- تشغيل دورة worker واحدة.
- Cron كل 10 دقائق.
- فشل مصدر واحد لا يوقف بقية المصادر.

أضف متغيرات قواعد البيانات في Railway Variables. لا تعتمد على وجود ملف `.env` داخل المستودع.

## ملاحظات المصادر

- MangaTek قد يرجع `403`؛ يتم تسجيله كمصدر متعذر دون إيقاف المصادر الأخرى.
- Manga Swat (`mangaswat`) يستخدم واجهة AppSwat الموجودة في `appswat.com`، مع روابط الأعمال والفصول العامة على `meshmanga.com`. يُحفظ المعرّف الرقمي للـ API كهوية العمل، ويُحفظ الـ slug كرابط عام. أثناء polling يستخدم المصدر endpoint تطبيق Manga Swat الموثّق لقسم «أحدث الفصول»: `https://appswat.com/v2/api/v1/series/releases/?page={page}&page_size=100`. envelope الاستجابة هو `count`/`next`/`previous`/`results`، والبطاقة الحالية تعرض `serie_id` و`title` و`slug` و`poster` و`rating` و`views_count` و`chapters`؛ كما يدعم parser أسماء نموذج الـ APK القديمة (`seriesId`/`name`/`latestReleasedChapters`) للتوافق. تُستخدم الفصول المضمّنة أثناء polling، ثم يجلب enrichment قائمة الفصول الكاملة من endpoint الصفحات المتعددة.
- الفصول المقفلة لا تُكسر. يبحث النظام عن نسخة متاحة في مصدر آخر ويراجع الفصل المقفل عند ظهور ملاحظة أحدث ذات صلة.

### توثيق endpoint أحدث الفصول في Manga Swat

تم تحديد endpoint أعلاه من نسخة APK الخاصة بـ Manga Swat، ثم قورنت استجابته الفعلية بعقد parser قبل اعتماده. نقاط التحقق التي يعتمد عليها الكود:

- واصف المسار `Lvc/y` يحتوي القيمة `series/releases`، واسمه النصي داخل التطبيق `SeriesLatestReleases`.
- تدفق الشاشة الرئيسية `Lkf/a` يستدعي هذا الواصف، ثم يمرر serializer باسم `LatestReleaseSeriesCardSerializer`.
- نموذج APK يعرّف الحقول `seriesId` و`name` و`slug` و`poster` و`latestReleasedChapters`.
- الاستجابة الحالية التي يعيدها الخادم تستخدم الحقول المكافئة `serie_id` و`title` و`slug` و`poster` و`chapters`، مع `rating` و`views_count`.
- نموذج الفصل المضمّن `LatestReleaseSeriesChapterItem` يعرّف `id` و`chapter` و`title` و`numberWithTitle`.
- طبقة الـ paging في الـAPK تضيف `page` و`page_size`، وتبدأ الصفحة الأولى بقيمة `page_size=100`.
- المسار `/v2/api/v2/series/` يبقى لمسار الفهرس/التفاصيل السابق، وليس مصدر قسم «أحدث الفصول».

التفاصيل الكاملة للعقد ومواقع الأدلة موجودة في
[`docs/MANGA_SWAT_ENDPOINT.md`](docs/MANGA_SWAT_ENDPOINT.md).
