{#
    dbt's default appends the custom schema to the target one, so a model
    tagged gridlens_gold would land in gridlens_silver_gold and dbt would try
    to create that database. Terraform owns the catalog here, so a custom
    schema is used exactly as written or not at all.
#}

{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
