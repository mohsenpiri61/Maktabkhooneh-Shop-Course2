from django.views.generic import (
    TemplateView,
    FormView,
    View, ListView
)
from django.contrib.auth.mixins import LoginRequiredMixin
from order.permissions import HasCustomerAccessPermission
from order.forms import CheckOutForm
from cart.models import CartModel, CartItemModel
from order.models import OrderModel, OrderItemModel, CouponModel, UserAddressModel, OrderStatusType
from django.urls import reverse_lazy
from cart.cart import CartSession
from django.http import JsonResponse
from django.utils import timezone
from django.shortcuts import redirect, get_object_or_404
from payment.zarinpal_client import ZarinPalSandbox
from payment.models import PaymentModel
from django.contrib import messages


class OrderCheckOutView(LoginRequiredMixin, HasCustomerAccessPermission, FormView):
    template_name = "order/checkout.html"
    form_class = CheckOutForm
    success_url = reverse_lazy('order:completed')

    def get_form_kwargs(self):
        """اضافه کردن درخواست کاربر به آرگومان‌های فرم."""
        kwargs = super(OrderCheckOutView, self).get_form_kwargs()
        kwargs['request'] = self.request
        return kwargs

    def form_valid(self, form):
        """عملیات مورد نیاز در صورت معتبر بودن فرم"""
        user = self.request.user
        cleaned_data = form.cleaned_data
        address = cleaned_data['address_id']
        coupon = cleaned_data['coupon']
        cart = CartModel.objects.get(user=user)
        
        
        # کنترل موجودی هر محصول در سبد خرید و کسر تعداد خریداری شده از تعداد موجودی محصول
        for item in cart.cart_items.all():
            if item.product.stock < item.quantity:
                form.add_error(None, f"موجودی محصول {item.product.title} کافی نیست.")
                return self.form_invalid(form)  
            else: 
                item.product.stock -= item.quantity
                item.product.save()   
                 
                
                 
        #  ایجاد سفارش و اضافه کردن آیتم های سفارش
        order = self.create_order(user, address, coupon)
        self.create_order_items(order, cart)         
         
        #  پاک کردن سبد خرید
        self.clear_cart(cart)
        
        # محاسبه قیمت نهایی
        order.total_price = order.calculate_total_price()

        # اضافه کردن کاربر به لیست استفاده‌کنندگان از کد تخفیف
        if coupon:
            coupon.used_by.add(user)
            coupon.save()
        order.save()
    
        # هدایت به درگاه پرداخت
        return redirect(self.create_payment_url(order))
        
    
    def create_payment_url(self, order):
        """ایجاد لینک پرداخت با استفاده از درگاه زرین‌پال"""
        print(f"Order Total Price Without consideration coupon: {order.total_price}")
        zarinpal = ZarinPalSandbox()
        authority = zarinpal.payment_request(order.get_price())
        print(f"Final Payable Price applying coupon: {order.get_price()}")

        # ذخیره اطلاعات پرداخت و ایجاد ارتباط بین سفارش و پرداخت، قبل از هدایت به درگاه
        payment_obj = PaymentModel.objects.create(authority_id=authority, amount=order.get_price())
        order.payment = payment_obj
        order.save()
        return zarinpal.generate_payment_url(authority)
    

    def create_order(self, user, address, coupon):
        """ایجاد سفارش جدید"""
        order =  OrderModel.objects.create(
            user=user,
            address=address,
            coupon=coupon
        )   
        order.save()
        return order
    
    
    def create_order_items(self, order, cart):
        for item in cart.cart_items.all():
            print(f"Product Stock Remainder:{item.product.stock}, Price: {item.product.get_price()}, Quantity: {item.quantity}")              
            OrderItemModel.objects.create(
                order=order,
                product=item.product,
                quantity=item.quantity,
                price=item.product.get_price(),
            )
            
            # محاسبه و ذخیره قیمت نهایی سفارش
            order.total_price = order.calculate_total_price() 
            order.save()
    

    def clear_cart(self, cart):
        """پاک کردن آیتم‌های سبد خرید"""
        cart.cart_items.all().delete()
        CartSession(self.request.session).clear()


    def form_invalid(self, form):
        return super().form_invalid(form)


    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        cart = CartModel.objects.get(user=self.request.user)
        total_price = cart.calculate_total_price()
        
        context.update({
            "addresses": UserAddressModel.objects.filter(user=self.request.user),
            "total_price": total_price,
            "total_tax": round((total_price * 9) / 100),
        })
        return context


class OrderCompletedView(LoginRequiredMixin, HasCustomerAccessPermission, TemplateView):
    template_name = "order/completed.html"
    
class OrderFailedView(LoginRequiredMixin, HasCustomerAccessPermission, TemplateView):
    template_name = "order/failed.html"


class ValidateCouponView(LoginRequiredMixin, HasCustomerAccessPermission, View):

    def post(self, request, *args, **kwargs):
        code = request.POST.get("code")
        user = request.user

        try:
            # استفاده از CouponModel برای دریافت کد تخفیف
            coupon = CouponModel.objects.get(code=code)

            # بررسی اعتبار کد تخفیف
            if coupon.expiration_date and coupon.expiration_date < timezone.now():
                return JsonResponse({"message": "کد تخفیف منقضی شده است"}, status=400)

            if coupon.used_by.count() >= coupon.max_limit_usage:
                return JsonResponse({"message": "محدودیت استفاده از کد تخفیف به پایان رسیده است"}, status=400)

            if user in coupon.used_by.all():
                return JsonResponse({"message": "شما قبلاً از این کد تخفیف استفاده کرده‌اید"}, status=400)

            # اعمال کد تخفیف
            cart = CartModel.objects.get(user=self.request.user)

            # محاسبه قیمت جدید
            total_price = cart.calculate_total_price()
            discount_price = total_price * (coupon.discount_percent / 100)
            final_price = total_price - discount_price
                           
            return JsonResponse({
                "message": "کد تخفیف اعمال شد",
                "total_price": round(final_price),
                "discount": round(discount_price)
            }, status=200)

        except CouponModel.DoesNotExist:
            return JsonResponse({"message": "کد تخفیف معتبر نیست"}, status=400)


class CancelCouponView(LoginRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        try:
            cart = CartModel.objects.get(user=request.user)
            cart.save()

            total_price = cart.calculate_total_price()

            return JsonResponse({
                "message": "کد تخفیف لغو شد",
                "total_price": round(total_price)
            }, status=200)

        except CartModel.DoesNotExist:
            return JsonResponse({"message": "سبد خرید شما خالی است"}, status=400)



    
class OrderPendingPaymentView(LoginRequiredMixin, View):
    '''پرداخت سفارش های در حالت PENDING'''
    def get(self, request, pk):
        order = get_object_or_404(OrderModel, pk=pk, user=request.user)

        # اطمینان از اینکه سفارش در حالت "در انتظار پرداخت" است
        if order.status != OrderStatusType.PENDING.value:
            messages.error(request, "این سفارش قابل پرداخت نیست.")
            return redirect('dashboard:customer:order-list')

        # هدایت به درگاه پرداخت 
        payment_url = f"https://sandbox.zarinpal.com/pg/StartPay/{order.payment.authority_id}/"
        return redirect(payment_url)    