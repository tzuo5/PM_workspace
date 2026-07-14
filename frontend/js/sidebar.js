/**
 * Sidebar interaction
 */
var Sidebar = (function() {

    function Sidebar() {
        this.sidebar = document.getElementById("sidebar");
        this.toggle = document.getElementById("sidebarToggle");
        this.init();
    }

    Sidebar.prototype.init = function() {
        var self = this;
        if (this.toggle) {
            this.toggle.addEventListener("click", function() {
                self.toggleSidebar();
            });
        }

        var navItems = document.querySelectorAll(".nav-item");
        navItems.forEach(function(item) {
            item.addEventListener("click", function(e) {
                e.preventDefault();
                navItems.forEach(function(i) { i.classList.remove("active"); });
                item.classList.add("active");

                var label = item.querySelector(".nav-label");
                document.dispatchEvent(new CustomEvent("sidebar:navigate", {
                    detail: {
                        section: item.getAttribute("data-section") || "",
                        title: label ? label.textContent.trim() : item.textContent.trim()
                    }
                }));
            });
        });
    };

    Sidebar.prototype.toggleSidebar = function() {
        this.sidebar.classList.toggle("collapsed");
        var icon = this.toggle.querySelector("svg");
        if (this.sidebar.classList.contains("collapsed")) {
            icon.style.transform = "rotate(180deg)";
        } else {
            icon.style.transform = "rotate(0deg)";
        }
    };

    return Sidebar;
})();
