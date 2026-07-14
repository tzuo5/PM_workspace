/**
 * PM Workplace - Project Tracker Data
 */

// Standard project workflow based on the seven email/attachment checkpoints
var ORDER_STAGES = [
    {
        id: "sales-contract",
        label: "销售开启合同",
        desc: "附件1：销售开启合同邮件"
    },
    {
        id: "pm-bt09",
        label: "PM开启BT09",
        desc: "附件2：PM开启合同BT09"
    },
    {
        id: "pa-so-bt09",
        label: "PA回复SO/BT09",
        desc: "附件3：Shelly（PA）开启BT09后回复SO号、BT09号"
    },
    {
        id: "iprocess",
        label: "iProcess审批",
        desc: "附件4：Mary/Icey（SA）开启iProcess审批流程"
    },
    {
        id: "book-order",
        label: "Book订单申请",
        desc: "附件5：PM确认预付款已收，Mary/Icey（SA）提交book订单申请"
    },
    {
        id: "factory-bt",
        label: "工厂BT回复",
        desc: "附件6：book完成后PM发Shelly下单，Shelly回复下单BT号"
    },
    {
        id: "factory-oa",
        label: "工厂反馈OA",
        desc: "工厂基于下单BT反馈OA，PM确认工厂反馈后项目可归档"
    },
    {
        id: "review-required",
        label: "需要人工审核",
        desc: "邮件与合同流程有关，但自动识别证据不足，需要人工确认"
    }
];

// Additional statuses (for tagging)
var SPECIAL_STATUSES = [
    { id: "suspended", label: "挂起", desc: "因客户原因/付款问题/审批卡点等暂停推进" },
    { id: "cancelled", label: "已取消", desc: "项目终止，不再执行" }
];

// Production starts with an empty dataset. Real projects are loaded from the local backend/SQLite.
var sampleOrders = [];

// Populate mock stage dates so the list view can render a Job Tracker-style progress line.
function addDaysForMockDate(dateValue, days) {
    var date = new Date(dateValue + "T00:00:00");
    if (isNaN(date.getTime())) return dateValue;
    date.setDate(date.getDate() + days);
    return date.toISOString().split("T")[0];
}

sampleOrders.forEach(function(order) {
    var activeIndex = ORDER_STAGES.findIndex(function(stage) { return stage.id === order.stage; });
    if (activeIndex < 0) activeIndex = 0;
    order.stageDates = order.stageDates || {};
    for (var i = 0; i <= activeIndex; i++) {
        var offset = (i - activeIndex) * 2;
        order.stageDates[ORDER_STAGES[i].id] = addDaysForMockDate(order.date, offset);
    }
});

// Empty state messages
var emptyStateMessages = {
    "sales-contract": { icon: "📄", title: "暂无销售合同", desc: "销售开启合同邮件后，项目会显示在这里" },
    "pm-bt09": { icon: "🧾", title: "暂无PM BT09", desc: "PM开启合同BT09后，项目会显示在这里" },
    "pa-so-bt09": { icon: "🔢", title: "暂无SO/BT09回复", desc: "PA回复SO号、BT09号后，项目会显示在这里" },
    "iprocess": { icon: "⚙", title: "暂无iProcess审批", desc: "SA开启iProcess审批流程后，项目会显示在这里" },
    "book-order": { icon: "📚", title: "暂无Book订单申请", desc: "预付款已收并提交book订单申请后，项目会显示在这里" },
    "factory-bt": { icon: "🏭", title: "暂无工厂BT回复", desc: "Shelly回复下单BT号后，项目会显示在这里" },
    "factory-oa": { icon: "📦", title: "暂无工厂反馈OA", desc: "工厂反馈OA后，项目可进入最终确认或归档" },
    "review-required": { icon: "?", title: "暂无待审核", desc: "低置信度邮件会集中到这里，确认后可拖到正确阶段" }
};
