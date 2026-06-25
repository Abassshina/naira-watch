from flask import Blueprint

product_bp = Blueprint("product", __name__)


@product_bp.route("/product/<product_name>")
def product_details(product_name):
    product_name = product_name.replace("-", " ")

    html = f"""
    <html>
    <head>
        <title>{product_name} | NairaWatch</title>

        <style>

            body{{
                font-family:Segoe UI;
                background:#f4f6f8;
                margin:0;
                color:#1f2937;
            }}

            .container{{
                max-width:1200px;
                margin:auto;
                padding:40px;
            }}

            .card{{
                background:white;
                border-radius:10px;
                padding:30px;
                box-shadow:0 3px 10px rgba(0,0,0,.08);
            }}

            .product-header{{
                display:flex;
                gap:40px;
            }}

            .image-box{{
                width:320px;
                height:320px;
                background:#eeeeee;
                display:flex;
                justify-content:center;
                align-items:center;
                font-size:90px;
                border-radius:10px;
            }}

            h1{
                margin-top:0;
            }

            .price{
                font-size:36px;
                color:#16a34a;
                font-weight:bold;
            }

            table{
                width:100%;
                border-collapse:collapse;
                margin-top:30px;
            }

            th,td{
                padding:15px;
                border-bottom:1px solid #ddd;
                text-align:left;
            }

            th{
                background:#0f172a;
                color:white;
            }

            .back{
                display:inline-block;
                margin-bottom:20px;
                text-decoration:none;
                color:#2563eb;
                font-weight:bold;
            }

            .coming{
                margin-top:40px;
                background:#eef6ff;
                padding:20px;
                border-radius:10px;
            }

        </style>

    </head>

    <body>

        <div class="container">

            <a class="back" href="/">← Back to Products</a>

            <div class="card">

                <div class="product-header">

                    <div class="image-box">

                        📱

                    </div>

                    <div>

                        <h1>{product_name.title()}</h1>

                        <div class="price">
                            Lowest Price Coming Soon
                        </div>

                        <p>
                            Product Intelligence page for
                            <strong>{product_name.title()}</strong>
                        </p>

                    </div>

                </div>

                <h2>Available Stores</h2>

                <table>

                    <tr>

                        <th>Store</th>

                        <th>Price</th>

                        <th>Status</th>

                    </tr>

                    <tr>

                        <td colspan="3">

                            This section will be connected to SQLite
                            in the next phase.

                        </td>

                    </tr>

                </table>

                <div class="coming">

                    <h3>Coming Soon</h3>

                    <ul>

                        <li>✅ Product Images</li>

                        <li>✅ Specifications</li>

                        <li>✅ Price History</li>

                        <li>✅ Trust Score</li>

                        <li>✅ Cheapest Store</li>

                        <li>✅ Price Drop Alerts</li>

                    </ul>

                </div>

            </div>

        </div>

    </body>

    </html>
    """

    return html